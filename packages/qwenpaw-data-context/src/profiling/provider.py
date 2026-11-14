"""探查引擎调用协议（ProfilingProvider）—— 算法实现与编排层之间的唯一契约。

对齐 data-profiling SPEC §5：
- 正式表与 staging 表的原始行由编排层按 datasource + 归属过滤后装进 ProbeContext.facts
  （一个 {键: DataFrame} 的 dict），算法只读消费。
- Candidate* 为算法 emit 的入参；上游引用一律用名称（domain_name/dataset_name/metric_name），
  由编排层（sink/promote）解析为正式 id。
- ProbeSink / ProfilingProvider 为 Protocol，算法侧实现同名 async 方法即可（类或模块级）。

Candidate* 的字段与值域是**受守卫的三层契约**（DTO ↔ staging 表 ↔ 正式表）：
改动本文件的任何 Candidate* 字段/值域，必须过
``tests/profiling/test_probe_candidate_contract.py`` —— 它把三层间的全部差异
（工程列/人工档/名称↔id 解析对/嵌套）显式登记并强制同步。

可空性分层：**DTO 严格（算法产出必须完整）→ staging 宽容（人工新增可
先缺）→ promote 终审（400 兜底，挡上传/人工路径）**。正式表 NOT NULL
的业务列（source / expr / time_window）在 DTO 上必填，违约在 emit
构造点即爆，不拖到两个阶段后的 promote。
"""

from __future__ import annotations

import asyncio
import importlib
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict

from profiling.config import get_settings


# ============================ 值域词表（线上契约正身） ============================
# 与算法侧词表（algo/snapshots/caliber.METRIC_TIME_WINDOWS、
# algo/handlers/caliber.ADDITIVITY_VALUES）的互钉由契约守卫测试断言。

TimeWindow = Literal["1d", "7d", "30d", "mtd"]
Additivity = Literal["additive", "semi_additive", "non_additive"]
DimensionRole = Literal["categorical", "time"]
#: L5 血缘列的取值 —— 只有 dimension / date 两个角色会产维度绑定
CaliberColumnRole = Literal["dimension", "date"]
#: 存储面布尔约定（④a）
YesNo = Literal["Y", "N"]
#: 候选来源：本轮是从零产出（create）还是完善既有草稿（refine）。落 staging 随行
#: 返给前端逐行展示；不 promote —— 正式表（confirmed 结果）没有「本轮」这个概念
Origin = Literal["create", "refine"]


# ============================ 候选结果 DTO（emit 入参） ============================
class _Candidate(BaseModel):
    """所有候选 DTO 的公共配置：多余字段一律拒绝 —— 键名 typo 在构造点爆，
    不能等到落库时变成静默 NULL（staging insert 对缺失键不报错）。"""

    model_config = ConfigDict(extra="forbid")


class CandidateMemberTable(_Candidate):
    source_table: str
    dataset_name: str | None = None


class CandidateBizDomain(_Candidate):
    """L1 业务域候选：domain_name 同时用于身份识别和展示，成员仅作探查证据。"""

    domain_name: str
    description: str | None = None
    member_tables: list[CandidateMemberTable] = []
    #: 新增还是完善（见 ``Origin``）。两种模式都以「新行落库、旧未锁定行删除」
    #: 的替换式写回，本字段随新行一起落 staging
    origin: Origin = "create"


class CandidateDatasetField(_Candidate):
    """L3 数据集字段候选。``is_primary`` / ``is_nullable`` 沿用存储面
    ``'Y'/'N'`` 约定（④a），promote 零转换。``description`` 是给列补业务
    语义的唯一入口（规范 L3）。"""

    field_name: str
    data_type: str | None = None
    is_primary: YesNo = "N"
    is_nullable: YesNo = "Y"
    description: str | None = None
    origin: Origin = "create"


class CandidateDataset(_Candidate):
    """L2 数据集候选（v2.1：单指针 ``source`` = 表名或 SQL，内容自判）。"""

    domain_name: str | None = None
    dataset_name: str
    description: str | None = None
    #: 表式来源写物理表全名（P3 两段全名同形态），SQL 式来源写 SQL 文本。
    #: 必填 —— 正式表 NOT NULL；投影侧（map_datasets）已按
    #: source → parents → dataset 名三级兜底，永不为空
    source: str
    #: 嵌套子候选（同 ``CandidateBizDomain.member_tables`` 先例）——
    #: 字段与 dataset 原子成对，Protocol 不必新增 emit 方法
    fields: list[CandidateDatasetField] = []
    origin: Origin = "create"


class CandidateDimensionCaliber(_Candidate):
    """L5 维度绑定候选（v2.1：expr 必填落库；value_example $$$ 多值）。"""

    domain_name: str | None = None
    dataset_name: str | None = None
    dimension_name: str
    #: 必填 —— 正式表 NOT NULL；口径表达式缺失时列名即表达式（投影侧兜底）
    expr: str
    #: $$$ 拼接的样例值串（规范红字：存在「全部」默认放入）
    value_example: str | None = None
    #: 内部血缘列（dimension / date），不 promote —— 保住时间列身份，
    #: DICT 回读时才能推出 L4 dimension_role=time；丢了会全落 categorical
    column_role: CaliberColumnRole | None = None
    #: 内部血缘列：物理列名，不 promote —— 读回时 DICT 聚合/归一按它，
    #: expr 是口径表达式不是列身份
    column_name: str | None = None
    origin: Origin = "create"


class CandidateMetricCaliber(_Candidate):
    """L7 度量口径候选（v2.1：expr+time_window 必填落库，time_window 进唯一键）。"""

    domain_name: str | None = None
    dataset_name: str | None = None
    metric_name: str
    #: 必填 —— 正式表 NOT NULL；D5：仅 prose 的行 expr 暂存 prose，不丢行
    expr: str
    #: 必填 + 值域四选一 —— 正式表 NOT NULL 且进唯一键
    time_window: TimeWindow
    additivity: Additivity | None = None
    #: 内部血缘列：物理列名（派生指标为日志别名），不 promote —— 读回时
    #: DICT 的 synonyms/归一按它，否则会把 SUM(...) 当成别名
    column_name: str | None = None
    origin: Origin = "create"


class CandidateDimension(_Candidate):
    """L4 维度字典候选（v2.1：parent_name/depth/enums 等退役；
    priority/child_dimensions 由 staging 默认值或人工编辑提供，算法不产）。"""

    domain_name: str | None = None
    dimension_name: str
    description: str | None = None
    synonyms: str | None = ""
    dimension_type: DimensionRole | None = None  # 算法内部 dimension_role 的边界映射
    origin: Origin = "create"


class CandidateMetric(_Candidate):
    """L6 度量字典候选（v2.1：unit/tags/is_polaris 等退役；priority 人工档不产）。"""

    domain_name: str | None = None
    metric_name: str
    description: str | None = None
    synonyms: str | None = None
    origin: Origin = "create"


# ============================ 运行期上下文 ============================
def _noop_cancel() -> None:  # 默认取消检查：不取消
    return None


async def _noop_progress(pct: int, message: str) -> None:  # 默认进度上报：丢弃
    return None


@dataclass
class ProbeContext:
    """编排层传给算法实现的只读上下文。

    ``facts`` 是**一个 dict 装完**的全部事实：正式表与 staging 表按
    datasource + 归属过滤后的原始行，值是 DataFrame。键的清单由算法侧
    声明（``profiling.algo`` 的 ``FACT_KEYS``），本层照单取数、不做整形 ——
    「行长什么样才算一份快照」属于算法侧的产出契约。

    这里曾是 9 个 pydantic DTO 列表 + 4 个 legacy 算法句柄。前者只有
    ``domain`` / ``dataset`` 两项有人读（sink 建 name→id 映射），后者只服务
    已下线的 ``algorithm_provider``，且其中两个是分钟级的远程预取。

    ``raise_if_cancelled()`` 供 CPU 密集循环协作式取消；
    ``report_progress(pct, message)`` 更新任务进度。
    """

    datasource_id: str
    datasource_type: str
    connection: dict  # 数据库连接信息
    principal: Any  # 项目鉴权信息

    #: {事实键: DataFrame}。见 ``profiling.algo.FACT_KEYS``
    facts: dict[str, Any] = field(default_factory=dict)
    #: 本次探查的模式：``create`` / ``rerun`` / ``refine``。
    #: 算法侧仅识别 create / refine（rerun 在 AlgoProvider 映射为 create）
    mode: str = "create"
    #: 每次执行独立目录，失活实例的遗留线程不能覆盖后续任务文件。
    artifact_id: str = field(default_factory=lambda: uuid4().hex)

    raise_if_cancelled: Callable[[], None] = _noop_cancel
    report_progress: Callable[[int, str], Awaitable[None]] = _noop_progress


class ProbeSink(Protocol):
    """算法把候选结果增量吐给编排层；编排层立即落 staging 并提交。"""

    async def emit_biz_domains(self, items: Sequence[CandidateBizDomain]) -> None: ...
    async def emit_datasets(self, items: Sequence[CandidateDataset]) -> None: ...
    #: 字段子表单独一刀 —— DATASET_FIELDS 是独立评审阶段，入参仍是 CandidateDataset
    async def emit_dataset_fields(self, items: Sequence[CandidateDataset]) -> None: ...
    async def emit_dimension_calibers(
        self, items: Sequence[CandidateDimensionCaliber]
    ) -> None: ...
    async def emit_dimension(self, items: Sequence[CandidateDimension]) -> None: ...
    async def emit_metric_calibers(
        self, items: Sequence[CandidateMetricCaliber]
    ) -> None: ...
    async def emit_metric(self, items: Sequence[CandidateMetric]) -> None: ...


class ProfilingProvider(Protocol):
    """算法实现本 Protocol（或提供同名模块级 async 函数）。
    约定：只“算 + emit”，不碰 DB、不管状态机、不捕获 CancelledError。"""

    async def probe_biz_domains(self, ctx: ProbeContext, sink: ProbeSink) -> None: ...
    async def probe_datasets(self, ctx: ProbeContext, sink: ProbeSink) -> None: ...
    async def probe_datasets_fields(
        self, ctx: ProbeContext, sink: ProbeSink
    ) -> None: ...
    async def probe_dimension_calibers(
        self, ctx: ProbeContext, sink: ProbeSink
    ) -> None: ...
    async def probe_dimensions(self, ctx: ProbeContext, sink: ProbeSink) -> None: ...
    async def probe_metric_calibers(
        self, ctx: ProbeContext, sink: ProbeSink
    ) -> None: ...
    async def probe_metrics(self, ctx: ProbeContext, sink: ProbeSink) -> None: ...


class CancelledProbe(asyncio.CancelledError):
    """协作式取消：raise_if_cancelled 命中时抛出（asyncio.CancelledError 子类）。"""


def load_provider() -> ProfilingProvider:
    """按 settings.probe_provider_path（`模块:属性`）动态加载算法 Provider。

    属性为类则实例化返回；为模块/对象则直接返回（需具备各阶段 probe_* async 方法）。
    """
    path = get_settings().probe_provider_path
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"非法 probe_provider_path：{path!r}，应为 '模块:属性'")
    module = importlib.import_module(module_name)
    obj = getattr(module, attr)
    return obj() if isinstance(obj, type) else obj
