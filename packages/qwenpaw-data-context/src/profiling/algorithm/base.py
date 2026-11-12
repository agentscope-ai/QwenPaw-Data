"""Profiling 框架的抽象定义（架构骨架）。

本模块只定义「有哪些角色、各自负责什么、它们如何协作」，不含任何具体算法或
IO 实现。真正的实现（AST 解析、SQL 抽取、pandas 处理、LLM 调用、xlsx 落盘等）
由各子类在 handler.py / llm.py / profiler_*.py / data_context.py / query_context.py
中继承实现。

------------------------------------------------------------------------
整体架构：黑板（blackboard）模式
------------------------------------------------------------------------
- TablesContext 是唯一的共享可变状态（黑板）。它既保存「源表原始结构」，
  也保存「每个任务产出的结果」——后一个任务从黑板上读取前一个任务写回的产物。
- QueryLogsContext 是一个只读的辅助证据源，负责把历史 SQL 日志解析成各任务需要
  的证据。可选：没有日志时它为 None，dataset 任务随之缺失，其余任务退化为「仅源表」。
- LlmClient 是纯粹的模型传输层（发请求 / 拿文本），不含任何任务语义。
- ProfileTaskHandler 是「每个任务一个」的策略对象，封装该任务的完整执行策略。
- Profiler 是编排器，对所有任务执行同一套三段式框架。

每个任务（biz_domain / dataset / dimension_caliber / dimension_manage / metric_caliber / metric_manage）
都走完全相同的流水线：

    table_evidence = source_tables.evidence_for(task)     # 从黑板读
    log_evidence   = query_logs.evidence_for(task)        # 从日志读（可能为空）
    task_results   = handler.handle(table_evidence, log_evidence)  # dict[str, DataFrame]
    source_tables.update(task_results, task)              # 一次写回黑板

所有 handler 的产物都是 ``dict[str, pd.DataFrame]``，:meth:`TablesContext.update`
按 :class:`ProfileTaskName` 决定写到黑板的哪一部分：
    - dimension_caliber / metric_caliber（见 :data:`TABLE_ARTIFACT_TASKS`）：
      键是 ``table_name``，更新各表列详情；可另带 :data:`OVERVIEW_ARTIFACT` 键
      更新表清单；
    - biz_domain：``{"biz_domain": 结果表, "domain_of": 单元格装着函数源码的表}``；
    - dataset / dimension_manage / metric_manage：``{task.value: 结果表}``。

普通任务使用 ProfileTaskHandler.handle 提供的单次 LLM 模板；业务域等存在覆盖率
校验和业务重试的任务只覆盖 handle，Profiler 主链路保持不变。

------------------------------------------------------------------------
如何扩展
------------------------------------------------------------------------
1. 实现 TablesContext / QueryLogsContext / LlmClient 各一个具体子类
   （pandas 版黑板见 data_context.SourceTablesContext；
   日志上下文见 query_context.QueryLogContext）。
2. 为 6 个任务各实现一个 ProfileTaskHandler 子类。普通任务实现
   system_prompt + build_user_prompt + parse_response；需要业务编排时覆盖 handle。
3. 实现 Profiler.handler_for，把 task 映射到对应 handler。
4. Profiler.profile / run_all 是已经写好的模板方法，通常无需重写。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeAlias

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. 任务枚举与执行顺序
# ===========================================================================


class ProfileTaskName(str, Enum):
    """Profiling / 源表版本任务名。

    值同时用于任务标识与 ``tables/{task}_{timestamp}/`` 目录命名。

    抓取阶段（fetch_* / add_enums）只写入源表黑板，不进入 :data:`TASK_ORDER`。
    Profiling 任务之间的依赖见 :data:`TASK_ORDER`：
        - biz_domain / dimension_caliber / metric_caliber 直接从源表 + 日志产出；
        - dataset 只能从日志产出（无源表证据）；
        - dimension_manage / metric_manage 读取 dimension_caliber / metric_caliber 富化后的
          列详情（is_dimension / is_metric / query_log_* 等标记）。
    """

    ORIGIN_OVERVIEW = "origin_overview"
    ORIGIN_SCHEMA = "origin_schema"
    ADD_ENUMS = "add_enums"

    LLM_DIMENSION_METRIC = "llm_dimension_metric"

    #: 维度 / 指标实体的第一步产物：纯聚合结果，LLM 补描述之前先落盘。
    AGG_DIMENSION = "agg_dimension"
    AGG_METRIC = "agg_metric"

    #: 以下 6 个与编排层 ``profiling.stages.STAGE_DEFS`` 的阶段一一同名。
    BIZ_DOMAIN = "biz_domain"
    DATASET = "dataset"
    DIMENSION_CALIBER = "dimension_caliber"
    DIMENSION_MANAGE = "dimension_manage"
    METRIC_CALIBER = "metric_caliber"
    METRIC_MANAGE = "metric_manage"


#: 默认执行顺序，编码任务间的依赖：先域与口径，再数据集预测，最后语义层。
TASK_ORDER: tuple[ProfileTaskName, ...] = (
    ProfileTaskName.BIZ_DOMAIN,
    ProfileTaskName.DIMENSION_CALIBER,
    ProfileTaskName.METRIC_CALIBER,
    ProfileTaskName.DATASET,
    ProfileTaskName.DIMENSION_MANAGE,
    ProfileTaskName.METRIC_MANAGE,
)

#: 仅依赖 query logs 的任务：缺少日志时应被 :meth:`Profiler.run_all` 跳过。
LOG_ONLY_TASKS: frozenset[ProfileTaskName] = frozenset({ProfileTaskName.DATASET})

#: 产物是「更新后的源表」的任务：产物字典的键是 table_name（外加
#: :data:`OVERVIEW_ARTIFACT`），写入黑板的表清单与各表列详情。
TABLE_ARTIFACT_TASKS: frozenset[ProfileTaskName] = frozenset(
    {
        ProfileTaskName.ORIGIN_OVERVIEW,
        ProfileTaskName.ORIGIN_SCHEMA,
        ProfileTaskName.ADD_ENUMS,
        ProfileTaskName.DIMENSION_CALIBER,
        ProfileTaskName.METRIC_CALIBER,
        ProfileTaskName.LLM_DIMENSION_METRIC,
    }
)

#: 表清单在产物字典中的保留键。
OVERVIEW_ARTIFACT = "tables_overview"

#: ``domain_of`` 源码在 domain 产物字典中的保留键，单列单行 DataFrame。
DOMAIN_RESOLVER_ARTIFACT = "domain_of"

#: :data:`DOMAIN_RESOLVER_ARTIFACT` DataFrame 里承载源码的列名。
DOMAIN_RESOLVER_COLUMN = "source"

#: 其余任务的产物字典只有一个键，即 ``task.value``（biz_domain 额外带 resolver）。

# ---------------------------------------------------------------------------
# TEMP：dimension / metric 仅处理 ``view_*`` 表。
# 撤回：将 :data:`DIM_METRIC_REQUIRE_VIEW_PREFIX` 改为 ``False`` 即可恢复
# 「dataset_* / view_* 优先，否则回退物理表」的旧行为。
# ---------------------------------------------------------------------------
DIM_METRIC_REQUIRE_VIEW_PREFIX: bool = True


def is_dim_metric_table_name(name: object) -> bool:
    """表名是否允许进入 dimension / metric 字段流水线。"""
    text = str(name)
    if DIM_METRIC_REQUIRE_VIEW_PREFIX:
        return text.startswith("view_")
    return text.startswith(("dataset_", "view_"))


# ===========================================================================
# 2. 证据与源表值对象
# ==========================================================================


@dataclass(frozen=True)
class SourceTableEvidence:
    """黑板为一个任务提供的全部只读上下文。

    字段说明：
        - ``table_overview``：表清单 DataFrame，列为
          ``table_name, table_comment[, merged_tables]``；
        - ``table_name_to_df_map``：``{table_name: 列详情 DataFrame}``，列详情随
          profiling 推进不断加宽（见 :class:`~.data_context.SourceTablesContext`）；
        - ``task_result``：本任务上一次写入的结果 DataFrame（没有则为空表）；
        - ``upstream``：``{task: 该任务的结果 DataFrame}``，即黑板上全部聚合产物；
        - ``domain_of``：黑板上已编译的 ``table_name -> domain_name`` 解析函数，
          biz_domain 任务尚未运行时为 ``None``；
        - ``sources``：仅供仍使用值对象的旧实现兼容。

    完整示例（metric_caliber 任务拿到的证据）::

        SourceTableEvidence(
            task=ProfileTaskName.METRIC_CALIBER,
            table_overview=<DataFrame: table_name, table_comment>,
            table_name_to_df_map={
                "ads_example_revenue_index_1d": <DataFrame: column_name,
                    data_type, column_comment, ..., is_dimension_in_query_logs>,
            },
            task_result=<空 DataFrame>,
            upstream={ProfileTaskName.BIZ_DOMAIN: <DataFrame: domain_name, ...>},
            domain_of=<function domain_of>,
        )
    """

    task: ProfileTaskName
    table_overview: Any = None
    table_name_to_df_map: dict[str, Any] = field(default_factory=dict)
    task_result: Any = None
    upstream: dict[ProfileTaskName, Any] = field(default_factory=dict)
    domain_of: Callable[[str], str | None] | None = None
    sources: tuple[Any, ...] = ()


@dataclass(frozen=True)
class LogEvidence:
    """查询日志针对一个任务聚合出的临时证据。"""

    task: ProfileTaskName
    records: Any


TableEvidence: TypeAlias = SourceTableEvidence


# ===========================================================================
# 3. 产物规范化辅助（handler 解析 LLM 结果 → 写回黑板的统一形态）
# ===========================================================================


def resolver_source_frame(source: str) -> Any:
    """把 ``domain_of`` 源码包装成单行单列 DataFrame，以便统一走产物字典。

    输出的结果示例（列 :data:`DOMAIN_RESOLVER_COLUMN`，仅一行）::

        source
        "def domain_of(table_name: str) -> str | None:\\n    ..."
    """
    import pandas as pd

    return pd.DataFrame([{DOMAIN_RESOLVER_COLUMN: str(source or "")}])


def resolver_source_of(frame: Any) -> str:
    """从 :func:`resolver_source_frame` 的 DataFrame 取回源码；取不到返回空串。"""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return ""
    column = (
        DOMAIN_RESOLVER_COLUMN
        if DOMAIN_RESOLVER_COLUMN in frame.columns
        else frame.columns[0]
    )
    value = frame.iloc[0][column]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def rows_to_dataframe(parsed: Any) -> Any:
    """把 handler 解析出的候选行规范为 DataFrame（延迟导入 pandas）。

    接受 ``DataFrame`` / ``list[dict | BaseModel]`` / 单个 ``dict | BaseModel`` /
    ``None``，统一返回 DataFrame；列名即各行的键，例如 dimension_manage 任务返回::

        domain_name,dimension_name,description,synonyms,enums
        sales,date_id,交易日期,日期、date,"[""20260101"", ""20260102""]"
    """
    import pandas as pd

    if isinstance(parsed, pd.DataFrame):
        return parsed
    if parsed is None:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, BaseModel):
                if hasattr(item, "model_dump"):
                    rows.append(item.model_dump(mode="json"))
                else:
                    rows.append(item.dict())
            elif isinstance(item, Mapping):
                rows.append(dict(item))
            else:
                rows.append({"value": item})
    elif isinstance(parsed, BaseModel):
        if hasattr(parsed, "model_dump"):
            rows.append(parsed.model_dump(mode="json"))
        else:
            rows.append(parsed.dict())
    elif isinstance(parsed, Mapping):
        rows.append(dict(parsed))
    else:
        rows.append({"value": parsed})
    return pd.DataFrame(rows)


# ===========================================================================
# 4. 运行配置（纯值对象）
# ===========================================================================


@dataclass(frozen=True)
class ProfileConfig:
    """一次 profiling 运行的全部参数。

    只承载「不可变配置」，不持有连接、句柄或算法状态——那些由 Profiler 及其
    协作对象持有。作为组合根 :func:`~.factory.build_profiler` 的输入。
    """

    # 编排层传入的是 datasource_id（TEXT）；本地跑批可直接给数字 id。
    datasource_id: str
    datasource_type: str  # MYSQL / POSTGRESQL / ODPS / HOLO ...

    # LLM 配置
    llm_model: str
    llm_api_key: str

    # query logs 抽取范围（无日志时可全为默认）
    dbname: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    query_max_rows: int = 100_000

    connection: dict = field(default_factory=dict)
    base_out_dir: Path = Path("data/profile")

    @property
    def out_dir(self) -> Path:
        db = self.dbname or "unknown_db"
        start = self.start_ts or "begin"
        end = self.end_ts or "end"
        return self.base_out_dir / f"{db}_{start}_{end}"


# ===========================================================================
# 5. 黑板：TablesContext
# ===========================================================================


class TablesContext(ABC):
    """全局上下文，同时是所有任务共享的「黑板」。

    职责：
        1. 保存源表的原始结构（表清单 / 列 / 枚举等）以及 profiling 过程中
           不断加宽的列详情；
        2. 保存与表无关的聚合产物：biz_domain / dataset / dimension_manage / metric_manage 各自的
           结果 DataFrame，以及 biz_domain 任务生成并编译好的 ``domain_of`` 函数；
        3. 为每个任务提供它需要的全部证据（:meth:`evidence_for`）；
        4. 用同一个 :meth:`update` 接收任何任务的产物，按 :class:`ProfileTaskName`
           决定写到上面哪一部分。

    产物统一为 ``dict[str, pd.DataFrame]``，键的含义按任务区分：
        - :data:`TABLE_ARTIFACT_TASKS`：键是 ``table_name``（列详情），可另带
          :data:`OVERVIEW_ARTIFACT` 键更新表清单；
        - 其余任务：键是 ``task.value``（该任务的结果表）；biz_domain 额外带
          :data:`DOMAIN_RESOLVER_ARTIFACT` 键承载 ``domain_of`` 源码。

    具体实现参考 data_context.py 中的 pandas 版 ``SourceTablesContext``。
    """

    @classmethod
    @abstractmethod
    def create_from_execute_sql(
        cls,
        execute_sql_func: Callable[[str], Any],
        out_dir: str | Path,
    ) -> "TablesContext":
        """通过 execute_sql 拉取全量源表结构并构建黑板初始状态。

        实现应负责：拉取表清单与列详情、探查枚举值、落盘缓存，并允许重复调用
        时复用缓存。
        """
        raise NotImplementedError

    @abstractmethod
    def evidence_for(self, task: ProfileTaskName) -> TableEvidence:
        """返回指定任务所需的全部证据，见 :class:`SourceTableEvidence`。

        各任务实际读取的部分：
            - domain：``table_overview``（表名与注释）；
            - dimension_caliber / metric_caliber：``table_name_to_df_map``；
            - dataset：不读源表，只用日志证据；
            - dimension_manage / metric_manage：``table_name_to_df_map`` 中被前两个任务标注过的
              ``is_dimension`` / ``is_metric`` 等列，外加 ``domain_of``。
        """
        raise NotImplementedError

    @abstractmethod
    def update(
        self,
        task_results: Mapping[str, Any],
        task: ProfileTaskName,
    ) -> None:
        """把任一任务的产物写回黑板，形成新版本，供后续任务读取。

        ``task_results`` 恒为 ``dict[str, pd.DataFrame]``，键的含义由 ``task``
        决定（见类文档）。实现需要按 ``task`` 路由到表清单 / 列详情 /
        任务结果 / ``domain_of`` 中对应的那一部分。
        """
        raise NotImplementedError

    @abstractmethod
    def artifacts_for(self, task: ProfileTaskName) -> dict[str, Any]:
        """返回该任务已经写入黑板的产物；从未运行过时返回空 dict。

        :meth:`Profiler.profile` 用它做断点续跑判断，因此「空 dict」必须严格表示
        「该任务没跑过」。产物存在不代表任务已完成，完成与否由
        :meth:`ProfileTaskHandler.is_complete` 判断。
        """
        raise NotImplementedError

    def has_version(self, task: ProfileTaskName) -> bool:
        """该任务是否已经有版本目录（哪怕里面还没有任何产物）。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持增量落盘")

    def open_version(self, task: ProfileTaskName) -> Any:
        """为逐表增量落盘开一个空版本，之后用 :meth:`checkpoint_table` 逐个填。

        这样版本目录里只会留下该阶段真正产出过的内容，而不是把黑板上已有的表
        全量复制一份。默认实现不支持，由具体黑板覆盖。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持增量落盘")

    def checkpoint_table(
        self,
        task: ProfileTaskName,
        table_name: str,
        frame: Any,
    ) -> Any:
        """把单张表的最新列详情立刻落盘到该任务当前版本目录。

        供逐表调用 LLM 这类长任务做增量持久化：每完成一张表就落一次，进程中断
        后重启可以只补没完成的表。要求该任务已经通过 :meth:`update` 建过版本。
        默认实现不支持增量落盘，由具体黑板覆盖。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持增量落盘")

    @property
    @abstractmethod
    def domain_of(self) -> Callable[[str], str | None] | None:
        """biz_domain 任务产出并编译好的 ``table_name -> domain_name`` 函数。"""
        raise NotImplementedError


# ===========================================================================
# 6. 辅助证据源：QueryLogsContext
# ===========================================================================


class QueryLogsContext(ABC):
    """历史查询日志上下文：所有「日志侧」证据抽取的唯一入口。

    设计要点：日志语料只解析一次（例如把每条 SQL 解析成一个 AST 分析对象并缓存），
    再由各 :meth:`evidence_for` 分支在同一份缓存上抽取不同证据，避免每个任务
    重复全量解析。

    具体实现参考 query_context.py 中的 pandas 版 ``QueryLogContext``。
    """

    @classmethod
    @abstractmethod
    def create_from_logs(
        cls,
        logs: list[str],
        query_logs_dir: str | Path,
    ) -> "QueryLogsContext":
        """从已有的 SQL 文本列表构建（例如来自 CSV）。"""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def create_from_execute_sql(
        cls,
        execute_sql_func: Callable[[str], Any],
        out_dir: str | Path,
        *,
        datasource_type: str,
        dbname: str,
        max_rows: int = 100_000,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> "QueryLogsContext":
        """通过 execute_sql 拉取历史日志（按 dbname / 时间范围）并构建。

        先看 ``out_dir/query_logs`` 有无缓存，没有再按 ``dbname`` 与时间范围拉取。
        """
        raise NotImplementedError

    @abstractmethod
    def evidence_for(self, task: ProfileTaskName) -> LogEvidence:
        """抽取指定任务的日志侧证据。

        原始日志证据本质上只有四类：
            - BIZ_DOMAIN → 聚合后的 JOIN 边、连通分量和表索引；
            - DATASET    → 规范化后的 SQL 模板；
            - DIMENSION_MANAGE（含 DIMENSION_CALIBER）→ GROUP BY / WHERE 中的维度列；
            - METRIC_MANAGE（含 METRIC_CALIBER）      → 聚合表达式。

        实现应在同一份已解析语料上分发到对应抽取逻辑（委托 profiler_*.py 中现有
        的自由函数即可，不必重写）。对没有日志证据的任务返回空。
        """
        raise NotImplementedError


# ===========================================================================
# 7. LLM 传输层：LlmClient
# ===========================================================================


class LlmClient(ABC):
    """纯粹的大模型调用封装，不含任何任务语义。

    只负责「发 prompt、拿文本」以及通用横切能力（重试、超时、prompt/response
    落盘等）。任务相关的 system prompt、user prompt 构造、结果解析都不在这里，
    而在各 :class:`ProfileTaskHandler`。
    """

    @abstractmethod
    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.8,
    ) -> str:
        """发送一次对话补全请求并返回模型文本。

        实现负责错误处理与（可选的）重试；不解析 JSON——解析交给 handler。
        """
        raise NotImplementedError


# ===========================================================================
# 8. 任务策略：ProfileTaskHandler
# ===========================================================================


class ProfileTaskHandler(ABC):
    """单个任务的策略对象：封装该任务独有的完整处理流程。

    「每个任务一套」的差异都收敛到这里，从而让 :meth:`Profiler.profile` 对所有任务
    保持同一套调用框架。普通任务可继承 handler.py 中的默认 LLM 流程；需要业务
    重试的任务（例如 Domain）可以覆盖 :meth:`handle`。
    """

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
    ) -> None:
        self.llm = llm
        self.out_dir = Path(out_dir) if out_dir is not None else Path(".")

    @property
    @abstractmethod
    def task(self) -> ProfileTaskName:
        """本 handler 服务的任务。"""
        raise NotImplementedError

    def build_system_prompt(self) -> str:
        """该任务的 system prompt（固定的角色与规则说明）。"""
        raise NotImplementedError

    def build_user_prompt(
        self,
        table_evidence: TableEvidence,
        log_evidence: LogEvidence | None,
    ) -> str:
        """把两类 evidence 转换为任务 user prompt。"""
        raise NotImplementedError

    def parse_response(self, response: str) -> Any:
        """把 LLM 响应转换为候选行 ``list[dict]``，由 :meth:`handle` 装箱。"""
        raise NotImplementedError

    def is_complete(self, artifacts: Mapping[str, Any]) -> bool:
        """黑板上已有的产物是否代表本任务已经跑完。

        默认「有产物即完成」。会做增量落盘的任务（metric_caliber 逐表判定列角色）
        覆盖此方法，用产物内容判断是否还有没做完的部分，从而支持断点续跑。
        """
        return bool(artifacts)

    def handle(
        self,
        table_evidence: TableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, Any]:
        """任务入口：拼装 prompt → 调用 LLM → 解析结果 → 装箱成产物字典。

        返回值恒为 ``dict[str, pd.DataFrame]``，直接交给
        :meth:`TablesContext.update`。默认实现把候选行装进 ``{task.value: df}``；
        产物形态不同的任务（biz_domain / dimension_caliber / metric_caliber）覆盖
        本方法。
        """
        system_prompt = self.build_system_prompt()
        user_prompt = self.build_user_prompt(table_evidence, log_evidence)
        logger.info("LLM call: task=%s (default handle)", self.task.value)
        response = self.llm.complete(system_prompt, user_prompt)
        return {self.task.value: rows_to_dataframe(self.parse_response(response))}


# ===========================================================================
# 9. 编排器：Profiler
# ===========================================================================


class Profiler(ABC):
    """profiling 编排器：对所有任务执行统一的三段式框架。

    :meth:`profile` 与 :meth:`run_all` 是已经写好的**模板方法**，体现整条流水线的
    逻辑，通常无需重写；子类只需实现 :meth:`handler_for`，把任务映射到具体
    :class:`ProfileTaskHandler`。协作对象（黑板 / 日志 / LLM）通过构造器注入。
    """

    def __init__(
        self,
        source_tables: TablesContext,
        query_logs: QueryLogsContext | None,
        llm: LlmClient,
    ) -> None:
        self.source_tables = source_tables
        self.query_logs = query_logs
        self.llm = llm

    @abstractmethod
    def handler_for(self, task: ProfileTaskName) -> ProfileTaskHandler:
        """返回指定任务的 handler。子类在此注册 6 个任务的实现。"""
        raise NotImplementedError

    def profile(self, task: ProfileTaskName) -> dict[str, Any]:
        """对单个任务执行统一的「读证据 → 合成 → 写回黑板」流水线。

        这套框架对所有任务完全一致：handler 返回 ``dict[str, pd.DataFrame]``，
        黑板按 task 决定这些 DataFrame 落到哪一部分。返回本次写入的产物字典；
        任务已经跑过时直接返回黑板上已有的产物，不重复调用 LLM。
        """
        self.raise_if_cancelled()

        handler = self.handler_for(task)
        if handler.task != task:
            raise ValueError(
                f"handler task mismatch: expected {task.value}, "
                f"got {handler.task.value}"
            )
        existing = self.source_tables.artifacts_for(task)
        if existing and handler.is_complete(existing):
            logger.info(
                "profile %s: 从磁盘/黑板加载已完成产物 keys=%s，跳过 LLM",
                task.value,
                sorted(str(k) for k in existing),
            )
            return existing

        logger.info("profile %s: 开始执行", task.value)
        table_evidence = self.source_tables.evidence_for(task)
        # log evidence 在空的情况下，也要能够正常执行
        optional_log_evidence = (
            self.query_logs.evidence_for(task) if self.query_logs is not None else None
        )

        task_results = handler.handle(table_evidence, optional_log_evidence)
        # 会增量落盘的任务在 handle 内部已经把产物写完了，这里不再重复写一版。
        # 无 dataset_*/view_ 逻辑表时口径任务可能返回空 dict，跳过 update 避免抛错。
        if task_results and not handler.is_complete(
            self.source_tables.artifacts_for(task)
        ):
            logger.info(
                "profile %s: 写回黑板 keys=%s",
                task.value,
                sorted(str(k) for k in task_results),
            )
            self.source_tables.update(task_results, task)
        else:
            logger.info("profile %s: 完成（产物已在 handler 内落盘或为空）", task.value)
        return task_results

    def run_all(self, tasks: tuple[ProfileTaskName, ...] = TASK_ORDER) -> None:
        """按依赖顺序执行全部任务；缺少日志时跳过仅依赖日志的任务。"""
        runnable_tasks = tuple(
            task
            for task in tasks
            if not (self.query_logs is None and task in LOG_ONLY_TASKS)
        )
        for index, task in enumerate(runnable_tasks):
            self.profile(task)
            self.report_progress(
                int((index + 1) / len(runnable_tasks) * 100),
                f"completed {task.value}",
            )

    # -- 运行期可选钩子：默认无操作，子类可覆盖以接入取消/进度上报 --

    def raise_if_cancelled(self) -> None:
        """若运行被取消则抛出；默认不做任何事。"""

    def report_progress(self, pct: int, message: str) -> None:
        """上报进度；默认不做任何事。"""
