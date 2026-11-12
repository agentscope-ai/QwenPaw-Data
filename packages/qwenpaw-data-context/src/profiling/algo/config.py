"""算法层的唯一参数入口。**一个类，一份清单。**

------------------------------------------------------------------------
命名
------------------------------------------------------------------------
::

    <消费模块>_<主体>_<方面>

**消费模块**是解释这个参数的那个包/文件，不是它「关于什么」——
所以 ``input_table_enums_workers`` 而不是 ``enum_workers``：读它的是
``inputs/table_input_sql.py``。前缀因此让「参数越界读取」grep 可查，
一个模块里出现别的模块的前缀就是越界。

包在前、包内模块在后，同一层的参数因此排在一起：

===================  ==================================================
``input_table_``     ``inputs/table_input_*.py``
``input_query_log_`` ``inputs/query_log_*.py``
``input_facts_``     ``inputs/probe_facts_input.py``
``parser_``          ``parser/``
``llm_``             ``handlers/llm.py`` + ``handlers/runner.py``
``domain_`` …        ``handlers/{domain,dataset,caliber,dictionary}.py``
``store_``           ``stores.py`` + ``contexts/``
===================  ==================================================

**方面**是闭集：``_enabled`` / ``_max_*`` / ``_min_*`` / ``_workers`` /
``_budget_seconds`` / ``_batch_size`` / ``_dir`` / ``_path`` /
``_patterns`` / ``_columns``。

两条例外，都因为没有单一消费模块：``datasource_id`` /
``datasource_connection`` / ``datasource_principal`` 是运行身份，
``execute_sql_path`` 是执行器注入点（由 ``load_*_execute_sql`` 装配）。

------------------------------------------------------------------------
优先级
------------------------------------------------------------------------
::

    构造参数  >  env / .env  >  宿主 config 的默认值  >  本文件的默认值

宿主同步走 :meth:`AlgoConfig._sync_host`：能 import 到 ``profiling.config`` /
``semantic_config.config`` 时，按**字段名或它声明的别名**去宿主取值补进来
——**只填空位**，所以 env 永远压得住宿主。宿主缺席则整段跳过。

**算法层的参数由算法层定义。** 只有 LLM 凭证从项目环境取，因此别名只有
两条（``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``，仓库 .env 的既有键）；
宿主的 ``probe_*`` 一族不再被本层认领，同步范围因此收窄到 ``llm_model``
与 ``llm_api_key`` 两个键。

------------------------------------------------------------------------
什么算参数
------------------------------------------------------------------------
**改了它，跑出来的结果不同，但代码依然正确** —— 那是参数，必须住这里。
algo 包内其他模块不得自带参数字面量。

不是参数，留在原地：

===========  =====================================================
契约         ``REQUIRED_*`` / ``*_KEY`` / ``EVIDENCE_KINDS`` / ``MANIFEST``
依赖图       ``TASK_ORDER`` / ``SUPPORTED_TASKS`` / ``OUTPUT_CONTEXT``
词表         ``METRIC_TIME_WINDOWS`` / ``COLUMN_ROLES``
方言         ``PG_SYSTEM_SCHEMAS`` / ``PG_DBNAME_KEYS`` / ``dialect_keys``（规则 5）
护栏         ``_MAX_PARENT_DEPTH`` / ``_MAX_UNWRAP_DEPTH``（防爆栈，不是旋钮）
版本         ``PARSER_VERSION``（改代码才改它，参与缓存指纹）
值契约       ``MAX_LIST_ITEMS``（落盘列表上限，须跨运行稳定否则缓存回读不一致）
===========  =====================================================

------------------------------------------------------------------------
用法
------------------------------------------------------------------------
唯一构造入口 ``AlgoConfig.build``：进程基线（env/宿主只读一次）+ 显式
字段，键必须是声明字段、值逐字段过校验 —— 没有别名、没有位置参数、
没有空值特判::

    config = AlgoConfig.build(
        datasource_id="42",
        datasource_type="postgresql",
        datasource_connection=cfg,
    )
    config = AlgoConfig.build(datasource_id="demo", input_query_log_dbname="shop")
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import AliasChoices, Field, TypeAdapter, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 宿主探测
# ---------------------------------------------------------------------------
#
# 这是整个 algo 包**唯一**向上的 import，全部 try 包裹、失败即降级、不产生
# 类型依赖（CLAUDE.md 规则 2 的唯一例外，边界因此仍是一个文件说得清的）。


@lru_cache
def _host() -> tuple[Any, str]:
    """宿主 settings 实例与它的模块名。就近优先，取不到则 ``(None, "")``。"""
    for name in ("profiling.config", "semantic_config.config"):
        try:
            return importlib.import_module(name).get_settings(), name
        except Exception:  # pragma: no cover - 取决于部署环境
            continue
    return None, ""


def _env_file() -> str:
    """宿主在则用 qwenpaw-data 的 env 文件；否则读 CWD 下的 .env。"""
    try:
        from qwenpaw_data.context.env import qwenpaw_data_env_file

        return str(qwenpaw_data_env_file())
    except Exception:  # pragma: no cover - 取决于部署环境
        return ".env"


def _local_today() -> date:
    """当前系统时区的自然日；显式时区避免 naive datetime 漂移。"""
    return datetime.now(UTC).astimezone().date()


def _names_of(name: str, field: FieldInfo) -> tuple[str, ...]:
    """字段名 + 它声明的 ``validation_alias``。

    别名只声明一处（``Field`` 上），env 读取与宿主同步共用同一份声明。
    """
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        return (name, *(c for c in alias.choices if isinstance(c, str)))
    if isinstance(alias, str):
        return (name, alias)
    return (name,)


#: 宿主配置的模块名；``""`` 表示独立模式。
HOST_CONFIG = _host()[1]
HOST_ENV_AVAILABLE = bool(HOST_CONFIG)


# ---------------------------------------------------------------------------
# 全部参数
# ---------------------------------------------------------------------------


class AlgoConfig(BaseSettings):
    """一次运行的全部参数。各层直接拿它，读自己那个前缀的字段。"""

    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # == 运行身份：跨模块，无单一消费方 =================================

    datasource_id: str = ""
    #: 通用数据源类型；表层与日志层都回落到它，特定覆盖见 input_* 两个字段
    datasource_type: str = "postgresql"
    #: datasource config JSON。「哪个键装着什么」是各消费者的方言知识
    #: （自建执行器取 db_path/host/…、日志方言取 dbname、JSON 输入取
    #: schema_dir），由各自从这里解析（规则 5），本层不解析
    datasource_connection: dict[str, Any] = Field(default_factory=dict)
    #: 提交探查时捕获的鉴权身份（编排层 ``ProbeContext.principal``）。
    #: 经 ``load_*_execute_sql`` 透传给 sql builder；判空与 ContextVar 绑定
    #: 在 ``sql.py`` 内完成。不落盘不进 env。
    datasource_principal: Any | None = Field(None, exclude=True)

    # == 表层来源（inputs/table_input_*.py）============================

    #: 表层特定覆盖；``None`` = 用通用 ``datasource_type``
    input_table_datasource_type: str | None = None
    #: 最多拉多少张表；``None`` 表示不限制
    input_table_max_count: int | None = None
    #: 排除的表名模式（SQL LIKE），默认排掉临时表
    input_table_exclude_patterns: tuple[str, ...] = ("tmp%",)
    #: 分区列候选：只探最新分区，避免全表扫描
    input_table_partition_columns: tuple[str, ...] = ("dt", "ds", "date_id")
    #: 把 xxx_20260101 这类分区表合并成一张逻辑表
    input_table_merge_partitions_enabled: bool = True

    #: 是否探查列枚举值，谨慎使用，耗费资源。
    input_table_enums_enabled: bool = False
    #: 单列 DISTINCT 上限；触到即认为「还有更多」，distinct 数记 None。
    #: 与 ``snapshots.MAX_LIST_ITEMS`` (=100) 配对：探到 101 个即「不止 100」，
    #: 落盘留 100 —— 改一个就要看另一个
    input_table_enums_max_distinct: int = 101
    #: 并发度。每列一条 SELECT DISTINCT，宽库是分钟级，并发不能省
    input_table_enums_workers: int = 8
    #: 探查总预算；用尽即带着已探到的部分返回，不阻塞整体
    input_table_enums_budget_seconds: float | None = 300.0

    # == 日志来源（inputs/query_log_*.py）==============================

    #: 日志层特定覆盖，**可与表层不同** —— 表在 Hologres，日志可能落在一张
    #: psycopg2 的本地镜像表里。``None`` = 用通用 ``datasource_type``
    input_query_log_datasource_type: str | None = None
    #: 显式库名；留空则方言侧从 ``datasource_connection`` 取
    input_query_log_dbname: str | None = None
    #: 最多拉多少条查询日志
    input_query_log_max_count: int = 1000
    #: 未显式给完整区间时，最多拉取最近多少个自然日（包含结束日）。
    #: 动态日期不能在字段默认值里求：``_baseline`` 是进程级缓存，长驻服务会
    #: 把窗口永久冻结在启动日；统一由 ``resolved_query_log_window`` 按任务求值。
    input_query_log_max_days: int = Field(90, gt=0)
    #: 抽取区间（yyyymmdd），两端都是**包含**语义，兼容已有 .env 配置。
    #: 两端都不配时默认最近 ``input_query_log_max_days`` 天；只配一端时，
    #: 另一端分别以结束日向前推窗口、或取当天。SQL 方言侧会把结束日换算成
    #: 次日的排他上界，避免时间戳列在结束日只命中 00:00:00。
    input_query_log_start_ts: str | None = None
    input_query_log_end_ts: str | None = None
    #: 文件日志来源（``input_query_log_datasource_type="sql_files"``）：
    #: spider2-lite 索引 jsonl 的路径（每行 ``{"instance_id", "db", ...}``），
    #: 按 ``db == input_query_log_dbname`` 过滤出本库的 SQL
    input_query_log_path: str | None = None
    #: 文件日志来源：gold SQL 目录，``{dir}/{instance_id}.sql`` 一文件一条
    input_query_log_dir: str | None = None

    # == 事实来源（inputs/probe_facts_input.py）=========================

    #: 按路径加载的事实实现（`模块:属性`）。曾指向编排层的
    #: ``SqliteProbeFactsInput`` —— 该实现已删除，默认清空。现行路径是
    #: 编排层直接交原始行（``services/context.py`` 的 ``_load_facts``），
    #: 本字段与 ``input_facts_db_path`` 为按路径加载的路线预留。
    input_facts_path: str = ""
    #: 按路径加载的实现自建连接用；生产路径应传 conn
    input_facts_db_path: str | None = None

    # == 解析（parser/）================================================

    parser_dialect: str = "postgres"
    #: 一个 SQL 模板重复几次才算 dataset 候选
    parser_template_min_count: int = 10
    #: dataset 模板至少需要的投影列数；``SELECT *`` 不受此限
    parser_template_min_columns: int = 5

    # == LLM 传输（handlers/llm.py + handlers/runner.py）===============

    llm_model: str = os.getenv("LLM_MODEL") or "gpt-4o-mini"
    #: 凭据由调用方显式注入，不认 env 别名。
    llm_api_key: str = ""
    llm_base_url: str = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    llm_temperature: float = 0.2
    llm_max_attempts: int = 2
    llm_concurrency: int = 4
    #: 产出语言（"zh" / "en"）。prompt 文本按语言选取；zh 文本逐字不动 ——
    #: prompt 参与 LLM 响应缓存摘要，改字即全量重跑。spider2 这类全英文
    #: 数据集用 "en"，domain / 业务名 / 描述随之全部输出英文
    llm_output_language: str = "zh"

    # == 运行模式（handlers/*.py）======================================

    #: handler 的工作模式，四个任务统一收（本阶段仅 DOMAIN 实现，其余接收即忽略）。
    #: ``create``：对未锁定的输入从零产出；``refine``：只完善既有草稿的文案
    #: （名称 / 展示名 / 描述），不增删、不动成员。编排层按用户点击的动作
    #: （探查 / 完善探查）注入，不进 env。
    handler_mode: Literal["create", "refine"] = "create"

    # == 任务阈值（handlers/{domain,dataset,caliber,dictionary}.py）====

    #: 覆盖率软门禁：低于此值先重试一次，仍不足则 warning 放行
    domain_min_coverage: float = 0.8
    dataset_batch_size: int = 20

    #: dataset 来源一：物理表直通。命中白名单的物理对象直接成为 dataset，
    #: 列元数据原样复制——直通没有投影可解析，不走 D1 继承
    dataset_derived_from_tables_enabled: bool = True
    #: 直通白名单，SQL LIKE 语法（``%`` 任意串、``_`` 单字符）。匹配的是**不带
    #: schema 的对象名**，且大小写不敏感——内存侧筛选，无需迁就 PG 的 LIKE 语义。
    #: 空元组 = 一张都不直通（白名单的常规语义，与 exclude 那一族相反）
    dataset_derived_from_tables_include_patterns: tuple[str, ...] = (
        "view%",
        "dataset%",
    )
    #: dataset 来源二：从高频 SQL 模板解析。两个来源都关等于没有 dataset 来源，
    #: DATASET 阶段直接抛错而不是产出空快照——静默的空会被读成「没探到」
    dataset_parse_from_logs_enabled: bool = True

    dict_batch_size: int = 40
    #: 单个条目进入**产物**的枚举/公式上限
    dict_output_max_enums: int = 50
    #: 单个条目进入 **Prompt** 的枚举样例上限
    dict_prompt_max_enums: int = 8
    #: 单个枚举样例在 Prompt 里的最大长度
    dict_prompt_max_enum_chars: int = 24
    #: 枚举值只是角色判定的参考，过长会挤占 prompt，按此长度截断
    caliber_prompt_max_enum_chars: int = 200

    # == 落盘（stores.py + contexts/）==================================

    #: 独立算法运行的落盘根目录；每个数据源一个子目录。
    #: 在线服务由 Provider 注入 store_datasource_dir，不使用此相对路径。
    store_out_dir: str = "data/profile"
    #: 宿主注入的完整数据源目录；优先于 store_out_dir，不再追加 datasource_id。
    store_datasource_dir: Path | None = None
    #: 人工核验用的 xlsx；关掉可省一半写盘时间
    store_excel_enabled: bool = True
    #: xlsx 单表行数上限；超出截断
    store_excel_max_rows: int = 100_000
    store_parquet_compression: str = "zstd"
    #: 只给这些 context 挂本地 store；空元组 = 全部落盘（默认）。
    #: 没挂 store 的 context 仍在内存里正常链接（save 只是不落盘），
    #: 代价是失去跨运行缓存。spider 这类一次性评测只要任务产物
    #: （domain / columns_caliber / columns_dict），输入层快照与直通
    #: dataset 落盘纯属浪费
    # TODO：需要支持对excel, parquet等不同格式的开关。
    store_include_contexts: tuple[str, ...] = ()

    # == 接线 ==========================================================

    #: 唯一的全局注入点，不属于任何模块。按 `模块:属性` 动态加载，
    #: 算法层因此不 import 编排层
    execute_sql_path: str = "profiling.sql_executor:execute_read"
    #: 直接注入的总执行器（三参数契约，见 ``profiling.algo.sql``）。
    #: 与 ``execute_sql_path`` 是同一件事的两种给法：函数直给 / 按路径加载；
    #: 设了函数就不再走路径。函数不落盘不进 env，仅构造参数可给
    execute_sql_func: Callable[..., Any] | None = Field(None, exclude=True)
    #: 注入执行器的行数硬上限。algo 按调用点传 ``max_rows``，其中 ``None``
    #: 表示「不限制」；而下游执行器只收 int，"不限"必须落成一个具体数字，
    #: 由本项兜底。原 ``semantic_config.probe_query_max_rows``，搬到执行器
    #: 参数该在的地方 —— 与 ``execute_sql_path`` 同主体、同消费者。
    execute_sql_max_rows: int = 100_000

    # -- 宿主同步 --------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _sync_host(cls, values: Any) -> Any:
        """把宿主声明过的同名（或同别名）键补进来。

        ``values`` 此时只含**显式给过**的键（构造参数 + env / .env）——
        pydantic-settings 的 source 只吐它真找到的键，默认值还没套上。
        因此这里只填空位：env 压得住宿主，宿主压得住本文件默认值。

        这条依赖 pydantic-settings 的取值时序（source 只吐它真找到的键，
        默认值在 ``mode="before"`` 之后才套上）。
        """
        host, _ = _host()
        if host is None or not isinstance(values, dict):
            return values
        for name, field in cls.model_fields.items():
            if name in values:
                continue
            for candidate in _names_of(name, field):
                if hasattr(host, candidate):
                    values[name] = getattr(host, candidate)
                    break
        return values

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def build(cls, **fields: Any) -> AlgoConfig:
        """唯一构造入口：进程基线 + 显式字段。

        基线（env / .env / 宿主配置）由 :func:`_baseline` 进程内只读一次，
        此后全部纯内存派生。``fields`` 的键必须是已声明字段 —— 未知键
        **直接报错**（``model_copy`` 本身会静默接受任意键，那正是「参数
        散落」的复发路径）；值逐字段过 pydantic 校验（``model_copy`` 不
        校验，键名与值类型因此都在本函数把关）。

        没有任何别名与空值特判：值可能为空的调用方自己决定给不给键。
        与直接构造的分工：``AlgoConfig(...)`` 走完整校验但每次重读 env
        （单测绕 env 用 ``AlgoConfig(_env_file=None)``）；``build`` 是
        缓存基线 + 校验增量。查询日志的相对窗口是唯一按本次运行物化的
        派生值，避免被进程基线冻结，也保证同一任务的 SQL 与指纹一致。
        """
        unknown = set(fields) - set(cls.model_fields)
        if unknown:
            raise KeyError(
                f"未声明的参数 {sorted(unknown)} —— 参数一律先在 AlgoConfig 里声明"
            )
        validated = {
            name: _adapter_of(name).validate_python(value)
            for name, value in fields.items()
        }
        config = _baseline().model_copy(update=validated)
        # 把滚动窗口钉在“本次 build”上：同一任务的缓存指纹与 SQL 必须看到
        # 完全相同的日期；下一次任务重新 build，又会按当天自然滚动。
        start_inclusive, end_inclusive, _ = config.resolved_query_log_window()
        return config.model_copy(
            update={
                "input_query_log_start_ts": start_inclusive,
                "input_query_log_end_ts": end_inclusive,
            }
        )

    # -- 派生 ------------------------------------------------------------

    def resolved_table_type(self) -> str:
        """表层数据源类型：特定覆盖优先，回落通用。小写归一。

        常规部署只填通用 ``datasource_type``；表层与日志层可以不是同一种，
        那时才显式给
        ``input_table_datasource_type`` / ``input_query_log_datasource_type``。
        解析只有这两个入口 —— 不存在第二处 ``or`` 兜底。
        """
        return (
            (self.input_table_datasource_type or self.datasource_type or "")
            .strip()
            .lower()
        )

    def resolved_query_log_type(self) -> str:
        """日志层数据源类型：特定覆盖优先，回落通用。小写归一。"""
        return (
            (self.input_query_log_datasource_type or self.datasource_type or "")
            .strip()
            .lower()
        )

    def resolved_query_log_window(
        self, *, today: date | None = None
    ) -> tuple[str, str, str]:
        """解析日志窗口，返回 ``(开始日, 结束日, 排他结束日)``。

        三个值都是 ``yyyymmdd``。前两个保留配置对外的双端包含语义，第三个
        专供 SQL 的半开区间 ``[start, end_exclusive)``。动态默认在调用时而
        不是进程基线初始化时求值，因此长驻服务跨天后会自然滚动。
        """

        max_days = self.input_query_log_max_days
        if max_days < 1:
            raise ValueError("input_query_log_max_days 必须大于 0")

        def parse(value: str, field: str) -> date:
            raw = value.strip()
            if len(raw) != 8 or not raw.isdigit():
                raise ValueError(f"{field} 必须是 yyyymmdd 格式，实际为 {value!r}")
            try:
                return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            except ValueError as exc:
                raise ValueError(
                    f"{field} 必须是有效的 yyyymmdd 日期，实际为 {value!r}"
                ) from exc

        end_inclusive = (
            parse(self.input_query_log_end_ts, "input_query_log_end_ts")
            if self.input_query_log_end_ts is not None
            else (today or _local_today())
        )
        start_inclusive = (
            parse(self.input_query_log_start_ts, "input_query_log_start_ts")
            if self.input_query_log_start_ts is not None
            else end_inclusive - timedelta(days=max_days - 1)
        )
        if start_inclusive > end_inclusive:
            raise ValueError(
                "input_query_log_start_ts 不能晚于 input_query_log_end_ts："
                f"{start_inclusive:%Y%m%d} > {end_inclusive:%Y%m%d}"
            )

        return (
            start_inclusive.strftime("%Y%m%d"),
            end_inclusive.strftime("%Y%m%d"),
            (end_inclusive + timedelta(days=1)).strftime("%Y%m%d"),
        )

    @property
    def datasource_out_dir(self) -> Path:
        """优先使用宿主注入目录；独立运行使用根目录 / 数据源 ID。

        按数据源分目录 —— 否则两个同库名的数据源会共用缓存。前缀规则：
        ``store_`` 开头 = 用户配置的根，``datasource_`` 开头 = 按身份派生。
        """
        if self.store_datasource_dir is not None:
            return self.store_datasource_dir
        return Path(self.store_out_dir) / str(self.datasource_id)

    @property
    def llm_enabled(self) -> bool:
        """无 api key 即按 D4 降级。判据只此一处。"""
        return bool(self.llm_api_key)

    def load_table_execute_sql(self) -> Callable[..., Any]:
        """表层 SQL 执行器：按 ``resolved_table_type()`` 装配，唯一入口。"""
        from .sql import default_execute_sql_func_builder

        return default_execute_sql_func_builder(
            self.resolved_table_type(),
            execute_sql=self.execute_sql_func,
            execute_sql_path=self.execute_sql_path,
            datasource_id=self.datasource_id,
            max_rows_floor=self.execute_sql_max_rows,
            connection=self.datasource_connection,
            principal=self.datasource_principal,
        )

    def load_query_log_execute_sql(self) -> Callable[..., Any]:
        """日志层 SQL 执行器：按 ``resolved_query_log_type()`` 装配，唯一入口。"""
        from .sql import default_execute_sql_func_builder

        return default_execute_sql_func_builder(
            self.resolved_query_log_type(),
            execute_sql=self.execute_sql_func,
            execute_sql_path=self.execute_sql_path,
            datasource_id=self.datasource_id,
            max_rows_floor=self.execute_sql_max_rows,
            connection=self.datasource_connection,
            principal=self.datasource_principal,
        )


@lru_cache
def _baseline() -> AlgoConfig:
    """进程基线：env / .env / 宿主配置只读一次，此后全部纯内存派生。

    不公开 —— 全局被共享的从来不是某个 config 对象，只是这份基线缓存；
    构造一律走 ``AlgoConfig.build``。单测要完全确定性时构造
    ``AlgoConfig(_env_file=None)``，绕开 .env。
    """
    return AlgoConfig()


@lru_cache
def _adapter_of(name: str) -> TypeAdapter:
    """按字段缓存的校验器。``model_copy`` 不校验，这里补上 —— 且只校验
    增量字段，不整体重建（重建会重读 env 源、丢掉 ``exclude`` 字段）。"""
    return TypeAdapter(AlgoConfig.model_fields[name].annotation)


def load_by_path(path: str) -> Any:
    """按 ``模块:属性`` 加载。与 ``profiling.provider.load_provider`` 同风格。

    算法层因此不必 import 编排层 —— 例如执行器的项目实现住在
    ``context_manager`` 侧，这里只按配置的路径取。
    """
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"非法路径 {path!r}，应为 '模块:属性'")
    return getattr(importlib.import_module(module_name), attr)


__all__ = [
    "HOST_CONFIG",
    "HOST_ENV_AVAILABLE",
    "AlgoConfig",
    "load_by_path",
]
