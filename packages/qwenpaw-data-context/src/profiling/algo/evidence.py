"""任务证据：各 context 的投影 + 装配 + 门禁。

------------------------------------------------------------------------
收集用循环，消费用字段
------------------------------------------------------------------------
profiler 遍历所有 context 收集（加 context 不用改 task，加 task 不用改
profiler）；但收集完要**装配成具名结构**再给 handler —— 否则 handler 拿到
一个异构列表，还得自己认领「哪个是表、哪个是日志」，而且每个 handler 都
要重写一遍那段认领代码。

------------------------------------------------------------------------
跨来源的合并不在这里，生命周期归并除外
------------------------------------------------------------------------
例如「日志证据 merge 进列元数据」是 CALIBER 自己的逻辑，归 handler。
放这里会让 evidence 层知道每个任务的内部算法。

同一产物的 staged / confirmed 不是两种业务来源，而是同一份事实的两个
生命周期。Domain 的正式实体是工作空间级目录，表归属却是当前数据源级增量；
Caliber 的正式关系是持久基线，staging CONFIRMED 则携带本轮增量与血缘。
所以下游必须在这里按各自自然键归并两侧，不能继续沿用整侧择一。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .snapshots import TableSnapshot
from .tasks import OUTPUT_CONTEXT, ProfileTask

logger = logging.getLogger(__name__)


# ===========================================================================
# 各 context 的投影结果
# ===========================================================================


@dataclass(frozen=True)
class TableEvidence:
    """表层。**物理表与 dataset 共用** —— 两者形状相同，这正是
    「表层可替换」的前提，消费者不需要区分来源。
    """

    snapshot: TableSnapshot

    @property
    def table_meta(self) -> pd.DataFrame:
        return self.snapshot.table_meta

    @property
    def column_meta(self) -> Mapping[str, pd.DataFrame]:
        return self.snapshot.column_meta

    def names(self) -> frozenset[str]:
        return self.snapshot.names()

    def is_empty(self) -> bool:
        return self.snapshot.is_empty()


@dataclass(frozen=True)
class LogEvidence:
    """日志证据。按 kind 索引；缺的 kind 是带列空表，下游不必判空。"""

    frames: Mapping[str, pd.DataFrame] = field(default_factory=dict)

    def of(self, kind: str) -> pd.DataFrame:
        frame = self.frames.get(kind)
        return frame if frame is not None else pd.DataFrame()

    def has(self, kind: str) -> bool:
        return len(self.of(kind)) > 0

    def is_empty(self) -> bool:
        return all(len(f) == 0 for f in self.frames.values())


@dataclass(frozen=True)
class DomainEvidence:
    """域归属。``members`` 是主体；``domains`` 携带当前生命周期的域元信息。"""

    members: pd.DataFrame
    domains: pd.DataFrame | None = None

    def domain_of(self, table_name: str | None) -> str | None:
        if not table_name or len(self.members) == 0:
            return None
        name = str(table_name).strip()
        lookup = dict(
            zip(self.members["table_name"].astype(str), self.members["domain_name"])
        )
        hit = lookup.get(name) or lookup.get(name.rsplit(".", 1)[-1])
        return str(hit) if hit else None

    def is_empty(self) -> bool:
        return len(self.members) == 0


@dataclass(frozen=True)
class CaliberEvidence:
    """列角色判定结果。DICT 按角色切分成维度、指标两条聚合线。"""

    calibers: pd.DataFrame
    # 纯离线流水线和同一 profiler 内刚生成的快照没有审核状态列，可以作为
    # 上游产物继续消费；在线 facts 回落到旧本地缓存时则必须为 False，避免
    # 把未确认的历史算法草稿误当成 CONFIRMED。
    allow_unstatused: bool = True

    def is_empty(self) -> bool:
        return len(self.calibers) == 0


@dataclass(frozen=True)
class SemanticCatalogEvidence:
    """工作空间内可复用的语义实体目录。

    目录只携带实体级字段，刻意不暴露 Dataset 级的公式、窗口、样例值或
    绑定关系。CALIBER 只能据此选择 canonical name，不能把其他数据源的
    口径复制到当前数据源。
    """

    dimensions: pd.DataFrame
    metrics: pd.DataFrame

    def is_empty(self) -> bool:
        return len(self.dimensions) == 0 and len(self.metrics) == 0


# ===========================================================================
# 装配结果
# ===========================================================================


@dataclass(frozen=True)
class TaskEvidence:
    """交给 handler 的全部只读上下文。frozen —— 要给下一个任务传信息，
    必须通过产物走一圈，不能就地改证据。
    """

    task: ProfileTask
    tables: TableEvidence | None = None  # 物理层（DOMAIN / DATASET）
    datasets: TableEvidence | None = None  # 逻辑层（CALIBER）—— 同一类型
    logs: LogEvidence | None = None
    domains: DomainEvidence | None = None
    calibers: CaliberEvidence | None = None
    semantic_catalog: SemanticCatalogEvidence | None = None
    # DATASET 补充分配只能选择正式 Domain；不能使用尚未晋升的 staging 定义。
    # 与 domains 的已确认成员分开，避免目录被 members 判空或混入 staging 新域。
    domain_catalog: pd.DataFrame | None = None

    #: 本任务**自己上一轮**的产出。staged 侧带行级 ``probe_status``
    #: （PENDING / CONFIRMED / SKIPPED）与 ``source_type``（ALGO / HUMAN）
    #: 列；confirmed 侧是已 promote 的 truth。与上面按「择一」合成的下游
    #: 字段不同：两侧**同时保留** —— 再生成要的是完整历史，由 handler 自己
    #: 区分「必须保留 / 不再提出 / 可以修订」。首跑或无事实时为 None。
    prior_staged: Any | None = None
    prior_confirmed: Any | None = None

    def has_log(self, kind: str) -> bool:
        """prompt 证据段是否渲染的判据。"""
        return self.logs is not None and self.logs.has(kind)

    def working_tables(self) -> TableEvidence | None:
        """本任务的主工作集：逻辑层优先，回落物理层。

        CALIBER 接的是 dataset。物理表要参与 CALIBER 时走的是
        ``dataset_derived_from_tables_enabled`` 直通——它们在 DATASET 环节就成了
        dataset，因此这里回落物理层只在 DOMAIN / DATASET 自身用得上。
        """
        return self.datasets or self.tables


@dataclass(frozen=True)
class Unavailable:
    """任务因硬依赖缺失而不可运行。**不是异常，是正常返回值。**"""

    task: ProfileTask
    reason: str


# ===========================================================================
# 装配 + 门禁
# ===========================================================================

#: 各任务的硬依赖：这些 context 的投影为空即不可运行。
#: 与 ``SUPPORTED_TASKS`` 的区别——那张表说「谁能提供」，这张说「谁必须有」。
#: 日志不在任何一条里：**它永远是增强项，缺失只降级不阻断**。
#: **键是 TaskEvidence 的字段名**，不是 context 名 —— 产出是成对的
#: （staged / confirmed），门禁要判的是「合成之后有没有」，不是某一侧有没有。
HARD_REQUIREMENTS: dict[ProfileTask, tuple[str, ...]] = {
    ProfileTask.DOMAIN: ("tables",),
    # DATASET 可以复用已确认归属，也可以基于正式目录补分配；由 handler
    # 对实际未归属表检查目录，不能在此要求 members 必须非空。
    ProfileTask.DATASET: ("tables",),
    ProfileTask.CALIBER: ("datasets",),
    ProfileTask.DICT: ("calibers",),
}

#: context 名 → (TaskEvidence 字段名, 优先级)。
#:
#: 普通字段有多个来源时**按优先级择一，小的赢**。整体择一而不是行级合并
#: —— stage 的 prereq 已经保证「上游 stage 走完（= 已 promote）才允许下游
#: 开跑」，所以下游看到的胜出侧应当是完整的一份。
#:
#: Domain 是例外：正式侧携带已有 Dataset 的归属，staged 侧携带本轮新表的
#: 已确认归属，两者都不是完整全集，必须按 ``table_name`` 合并。该特判在
#: :func:`_merge_domain_evidence`，因此不登记在本表中。
#:
#: 其余通用规则：**confirmed 压过 staged**。
#:
#: Caliber 也不登记在这里：DICT 需要正式关系 + 本轮已确认 staging 关系按
#: 自然键合并，而不是整侧择一。该特判在 :func:`_merge_caliber_evidence`。
_FIELD_OF: dict[str, tuple[str, int]] = {
    "source_table": ("tables", 0),
    "query_log": ("logs", 0),
    "dataset_confirmed": ("datasets", 0),
    "dataset_staged": ("datasets", 1),
    # 只有 confirmed DictContext 会为 CALIBER 投影此证据；staged catalog
    # 不可跨数据源复用，也不登记为回退来源。
    "columns_dict_confirmed": ("semantic_catalog", 0),
}

_DOMAIN_PARTS = frozenset({"domain_confirmed", "domain_staged"})
_CALIBER_PARTS = frozenset({"columns_caliber_confirmed", "columns_caliber_staged"})
_CONFIRMED = "CONFIRMED"

# 正式关系不保存物理列血缘；staging 中这两列用于把正式口径重新投影给 DICT。
# 同一自然键两侧同时存在时，除此之外的字段一律以正式持久值为准。
_CALIBER_LINEAGE_FIELDS = ("column_name", "column_role")


def _confirmed_rows(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """staged 帧只向下游暴露 CONFIRMED 行；无状态列的离线快照原样保留。"""
    if frame is None or "probe_status" not in frame.columns:
        return frame
    status = frame["probe_status"].map(
        lambda value: str(value).strip().upper() if value is not None else ""
    )
    return frame.loc[status == _CONFIRMED].copy()


def _confirmed_caliber_rows(value: CaliberEvidence | None) -> pd.DataFrame | None:
    """按证据来源决定无状态 CALIBER 快照能否视作已确认。"""
    if value is None:
        return None
    frame = value.calibers
    if "probe_status" not in frame.columns and not value.allow_unstatused:
        return frame.iloc[0:0].copy()
    return _confirmed_rows(frame)


def _text(value: Any) -> str | None:
    """归并键统一成去空白字符串；NULL / NaN 不参与冲突判定。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _merge_member_frames(frames: list[pd.DataFrame | None]) -> pd.DataFrame:
    """按表合并成员；同表指向不同 Domain 是契约冲突，必须显式失败。"""
    available = [frame for frame in frames if frame is not None and len(frame) > 0]
    if not available:
        # DomainEvidence 的成员帧按契约至少有这两列；带列空表让下游查表稳定。
        return pd.DataFrame(
            {
                "table_name": pd.Series(dtype="object"),
                "domain_name": pd.Series(dtype="object"),
            }
        )

    merged = pd.concat(available, ignore_index=True, sort=False)
    domain_by_table: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for row in merged.to_dict(orient="records"):
        table_name = _text(row.get("table_name"))
        domain_name = _text(row.get("domain_name"))
        if table_name is None or domain_name is None:
            # ``DomainEvidence.domain_of`` 最终会构造 table → domain 的 dict。
            # 缺自然键的行若留下，可能在同表后出现时把正式有效映射静默覆盖成
            # 空值；因此这里把它当作无效证据丢弃，而不是让门禁误判为可用。
            logger.warning("忽略缺少 table_name/domain_name 的 Domain 成员: %r", row)
            continue
        previous = domain_by_table.get(table_name)
        if previous is not None and previous != domain_name:
            raise ValueError(
                "Domain 归属冲突："
                f"表 {table_name!r} 同时属于 {previous!r} 与 {domain_name!r}"
            )
        if previous is None:
            # 判重键和交给下游的值必须是同一规范形态，否则 formal 脏值会
            # 赢得优先级，却无法被 ``domain_of('clean_name')`` 命中。
            row["table_name"] = table_name
            row["domain_name"] = domain_name
            normalized.append(row)
        domain_by_table[table_name] = domain_name
    return pd.DataFrame(normalized, columns=merged.columns)


def _merge_domain_frames(frames: list[pd.DataFrame | None]) -> pd.DataFrame | None:
    """合并 Domain 元数据；正式侧在前，同名时保留正式定义。"""
    available = [frame for frame in frames if frame is not None and len(frame) > 0]
    if not available:
        return None
    merged = pd.concat(available, ignore_index=True, sort=False)
    if "domain_name" not in merged.columns:
        return merged
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in merged.to_dict(orient="records"):
        name = _text(row.get("domain_name"))
        if name is None:
            logger.warning("忽略缺少 domain_name 的 Domain 元数据: %r", row)
            continue
        if name in seen:
            continue
        row["domain_name"] = name
        normalized.append(row)
        seen.add(name)
    return pd.DataFrame(normalized, columns=merged.columns)


def _merge_domain_evidence(
    confirmed: DomainEvidence | None,
    staged: DomainEvidence | None,
) -> DomainEvidence | None:
    """下游 Domain 证据 = formal truth + 本轮已确认 staging 增量。"""
    if (
        staged is not None
        and staged.domains is not None
        and len(staged.domains) > 0
        and len(staged.members) > 0
    ):
        domains_have_status = "probe_status" in staged.domains.columns
        members_have_status = "probe_status" in staged.members.columns
        if domains_have_status != members_have_status:
            raise ValueError(
                "Domain staging 生命周期列不一致："
                "domains 与 members 必须同时带 probe_status，或同时不带"
            )
    staged_members = _confirmed_rows(staged.members) if staged is not None else None
    staged_domains = _confirmed_rows(staged.domains) if staged is not None else None
    members = _merge_member_frames(
        [
            confirmed.members if confirmed is not None else None,
            staged_members,
        ]
    )
    domains = _merge_domain_frames(
        [
            confirmed.domains if confirmed is not None else None,
            staged_domains,
        ]
    )
    merged = DomainEvidence(members=members, domains=domains)
    return None if merged.is_empty() else merged


def _caliber_natural_key(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """返回关系自然键；缺关键名称的异常行不参与归并，但仍会原样下传。"""
    domain_name = _text(row.get("domain_name"))
    table_name = _text(row.get("table_name"))
    dimension_name = _text(row.get("dimension_name"))
    metric_name = _text(row.get("metric_name"))
    if domain_name is None or table_name is None:
        return None
    if dimension_name is not None:
        return ("dimension", domain_name, table_name, dimension_name)
    if metric_name is not None:
        return (
            "metric",
            domain_name,
            table_name,
            metric_name,
            _text(row.get("time_window")),
        )
    return None


def _present(value: Any) -> bool:
    """血缘补值判空；兼容 pandas 标量与列表值。"""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return not isinstance(value, str) or bool(value.strip())


def _merge_caliber_evidence(
    confirmed: CaliberEvidence | None,
    staged: CaliberEvidence | None,
) -> CaliberEvidence | None:
    """DICT 口径证据 = formal truth + staging 中已确认的当前源增量。

    相同自然键以正式行的持久字段为准；正式表没有的 ``column_name`` 与
    ``column_role`` 从 staging 补回。PENDING / SKIPPED staging 行不向 DICT
    暴露。无 ``probe_status`` 的离线快照按既有约定视作可用。
    正式行的自然键必须唯一，重复时在展开 staging 血缘前拒绝。
    """
    confirmed_frame = confirmed.calibers if confirmed is not None else None
    staged_frame = _confirmed_caliber_rows(staged)
    available = [
        frame
        for frame in (confirmed_frame, staged_frame)
        if frame is not None and len(frame) > 0
    ]
    if not available:
        return None

    formal_records = (
        confirmed_frame.to_dict(orient="records") if confirmed_frame is not None else []
    )
    # 即便没有 staging，也不能让重复正式关系进入 DICT 后被当成合法成员。
    # 与 promote.complete 一致，内容完全相同的正式重复行也视为歧义。
    formal_positions: dict[tuple[Any, ...], int] = {}
    for position, row in enumerate(formal_records, start=1):
        key = _caliber_natural_key(row)
        if key is None:
            continue
        if key in formal_positions:
            raise ValueError(
                f"正式口径自然键重复：{key!r}；"
                f"证据第 {formal_positions[key]}、{position} 行对应同一正式关系。"
                "请先在正式资产管理中消除重复记录，再重试建模"
            )
        formal_positions[key] = position

    staged_records = (
        staged_frame.to_dict(orient="records")
        if staged_frame is not None and len(staged_frame) > 0
        else []
    )
    staged_by_key: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(staged_records):
        key = _caliber_natural_key(row)
        if key is not None:
            staged_by_key.setdefault(key, []).append((index, row))

    merged_records: list[dict[str, Any]] = []
    consumed_staged: set[int] = set()
    if formal_records:
        for formal in formal_records:
            key = _caliber_natural_key(formal)
            matches = staged_by_key.get(key, ()) if key is not None else ()
            if not matches:
                merged_records.append(dict(formal))
                continue

            # 一条正式关系可对应多条 staging 列血缘，必须逐条补回后供 DICT
            # 聚合，不能 first-wins 拼成混合血缘。staging 持久字段的冲突
            # 由 promote.complete 在正式落库前校验。
            for index, candidate in matches:
                merged = dict(formal)
                for field_name in _CALIBER_LINEAGE_FIELDS:
                    value = candidate.get(field_name)
                    if _present(value):
                        # formal 的 column_name/column_role 都是读侧 fallback，
                        # 并非持久字段；命中 staging 时必须用真实血缘覆盖。
                        merged[field_name] = value
                merged_records.append(merged)
                consumed_staged.add(index)

    # 当前源尚未 promote 的已确认关系作为增量保留；同键但无正式基线的多条
    # 冲突确认行不在这里 first-wins，留给 promote.complete 在正式落库前报错。
    merged_records.extend(
        row for index, row in enumerate(staged_records) if index not in consumed_staged
    )

    columns = list(
        dict.fromkeys(column for frame in available for column in frame.columns)
    )
    return CaliberEvidence(pd.DataFrame(merged_records, columns=columns))


def assemble(
    task: ProfileTask,
    parts: Mapping[str, Any],
) -> TaskEvidence | Unavailable:
    """把 ``{context 名: 投影}`` 装配成具名结构，并做门禁。

    **先按优先级合成，再判空** —— 成对的产出只要有一侧非空就算「有」。
    硬依赖为空 → :class:`Unavailable`；增强项为空 → warning，继续。

    **自身产出对先路由**（``OUTPUT_CONTEXT[task]`` 及其 confirmed 孪生）——
    这一步必须早于通用判空：DOMAIN 的正式目录可能有域行但尚无当前源成员，
    此时 ``DomainEvidence.is_empty()`` 仍为真，却正是 DOMAIN 复用所需的 prior。
    自身产出对不走
    ``_FIELD_OF`` 的择一规则，而是两侧原样路由进 ``prior_staged`` /
    ``prior_confirmed`` —— 再生成要的是完整历史；也避免 DOMAIN 自己的
    ``domains`` 字段被上一轮产出污染。prior 永远是增强项，不进门禁。
    """
    own_staged = OUTPUT_CONTEXT[task]
    own_confirmed = own_staged.removesuffix("_staged") + "_confirmed"

    fields: dict[str, Any] = {}
    chosen: dict[str, int] = {}
    domain_parts: dict[str, DomainEvidence] = {}
    caliber_parts: dict[str, CaliberEvidence] = {}
    for name, value in parts.items():
        # 必须先于 _empty：DomainEvidence 以 members 判空，但 DOMAIN 自身还要
        # 读取只有 domains 元数据、尚无当前源成员的全局目录。
        if name == own_staged:
            if value is not None:
                fields["prior_staged"] = value
            continue
        if name == own_confirmed:
            if value is not None:
                fields["prior_confirmed"] = value
            continue

        # Domain 的两个生命周期不是互斥全量，而是 formal 基线 + staged 增量。
        # 先收齐，循环结束后统一过滤状态、校验冲突并按表归并。
        if name in _DOMAIN_PARTS and isinstance(value, DomainEvidence):
            domain_parts[name] = value
            continue

        # DICT 的口径证据同样是两侧增量归并：formal 持久字段为准，staging
        # 只取 CONFIRMED，并补足正式表没有的列血缘。
        if name in _CALIBER_PARTS and isinstance(value, CaliberEvidence):
            caliber_parts[name] = value
            continue

        if _empty(value):
            continue
        mapped = _FIELD_OF.get(name)
        if mapped is None:
            continue
        field_name, priority = mapped
        if priority < chosen.get(field_name, priority + 1):
            fields[field_name] = value
            chosen[field_name] = priority

    domains = _merge_domain_evidence(
        domain_parts.get("domain_confirmed"),
        domain_parts.get("domain_staged"),
    )
    if domains is not None:
        fields["domains"] = domains
    if task == ProfileTask.DATASET:
        formal_domains = domain_parts.get("domain_confirmed")
        if formal_domains is not None:
            fields["domain_catalog"] = formal_domains.domains

    calibers = _merge_caliber_evidence(
        caliber_parts.get("columns_caliber_confirmed"),
        caliber_parts.get("columns_caliber_staged"),
    )
    if calibers is not None:
        fields["calibers"] = calibers

    missing = [
        field_name
        for field_name in HARD_REQUIREMENTS.get(task, ())
        if field_name not in fields
    ]
    if missing:
        return Unavailable(task, f"缺少必需的上游产出: {', '.join(missing)}")

    if "logs" not in fields:
        logger.info("%s: 无查询日志证据，降级运行（不阻断）", task.value)

    return TaskEvidence(task=task, **fields)


def _empty(value: Any) -> bool:
    if value is None:
        return True
    is_empty = getattr(value, "is_empty", None)
    return bool(is_empty()) if callable(is_empty) else False


__all__ = [
    "HARD_REQUIREMENTS",
    "CaliberEvidence",
    "DomainEvidence",
    "LogEvidence",
    "SemanticCatalogEvidence",
    "TableEvidence",
    "TaskEvidence",
    "Unavailable",
    "assemble",
]
