"""DOMAIN：JOIN 聚类 + LLM 划分业务域（create）/ 完善既有草稿（refine）。

流程移植自 legacy ``BizDomainTaskHandler``：装配 cluster 证据 → 一次调用 →
覆盖率不足或校验失败则带上次响应整体重试一次 → 取覆盖率高者 →
普通 DOMAIN 低于 ``MIN_DOMAIN_COVERAGE`` 记 warning（软门禁，不阻断）；
Dataset 总体覆盖率必须大于该阈值，重试后仍未超过则失败。

与 legacy 的两点差异（均已拍板）：

- **D2**：不再让 LLM 生成 ``domain_of`` Python 代码再 ``exec``。模型直接给
  每个 domain 的成员表清单，物化成 ``DomainSnapshot.members`` 数据行——
  members 表就是那个函数的物化形式（见 snapshots/domain.py 的设计说明）。
- **不再按「日志见过」过滤源表**：legacy 的判据是逐条 SQL 的
  ``physical_table_names`` 全集，新证据面只有 join 边，判据不可得；
  表范围收窄由上游 ``exclude_table_patterns`` 承担。

并查集聚类从 parser 的「下游职责」在这里兑现——snapshot 只存边表，
聚类是 DOMAIN 的输入装配，不是日志证据。

------------------------------------------------------------------------
两种模式，一条同构流水线
------------------------------------------------------------------------
::

    ① 输入集合   create: 未锁定表 = 全部表 − 已确认归属的成员表
                 refine: staging 中全部 PENDING 草稿（ALGO / HUMAN，含零成员；带 draft_id）
    ② 既有证据   Domain 目录（正式表 + staging CONFIRMED）作为可复用上下文；
                 当前源已确认成员表冻结；完整表注释；
                 create 另有 join 聚类（只在未锁定表上算）
    ③ LLM        create: 在输入表上优先复用已有域，无匹配才创建新域
                 refine: 逐草稿完善 domain / display_name / description；
                 已有成员强制不动，零成员草稿可从未分配表补成员
    ④ 兜底       锚定已有域精确名与元信息、剥离锁定表、同名产出合并；
                 （create）删空域
                 refine 附加：按 draft_id 对齐；有成员则强制恢复；零成员接受分配；
                 未回带的草稿进入同一确定性收口，draft_id 未知的产出丢弃
    ⑤ 输出       只含本轮产出 —— 已确认成员原地保留、不 re-emit；
                 复用域仍输出一条 domain 定义及本轮新成员；
                 domains 帧带 ``origin`` 额外列（create / refine）

skip 行不是输入（skip = 删除）；未确认草稿（含 HUMAN）走替换式写回
（新行落库、旧未锁定行删除）由编排层完成，本层只管产出。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..evidence import TaskEvidence
from ..snapshots import DomainSnapshot
from .base import TaskHandler
from .llm import AsyncLlmClient, extract_json_payload
from .prompts import (
    build_domain_retry_prompt,
    build_domain_system_prompt,
    build_domain_user_prompt,
)
from .runner import LlmRunner, LlmUnit

logger = logging.getLogger(__name__)

#: DOMAIN 按待分配表计算，达到阈值即通过，重试仍不足则告警放行；
#: Dataset 按全部输入表计算，必须严格超过阈值，重试仍未超过则失败。
MIN_DOMAIN_COVERAGE = 0.8

MODE_CREATE = "create"
MODE_REFINE = "refine"

_DOMAIN_NAME_RE = re.compile(r"[a-z0-9_]+")

#: staging 行状态 / 来源的取值约定 —— ``probe_facts_input`` 把它们作为
#: 额外列随 staged 快照带进来（与编排层 ``profiling/models/probe.py`` 同值，
#: 但算法层是叶子库，不 import 宿主常量）。
_STATUS_CONFIRMED = "CONFIRMED"
_STATUS_PENDING = "PENDING"


@dataclass(frozen=True)
class _PriorState:
    """DOMAIN 既有状态的两种正交语义。

    ``catalog`` 是 workspace 范围的可复用 Domain 目录；名字存在于目录中不代表
    本轮不能再把新表归入它。``locked_table_owner`` 才是当前数据源已经确认的
    表归属，键中的表不得被本轮移动或重复产出。
    """

    catalog: dict[str, dict[str, Any]]
    locked_table_owner: dict[str, str]


def _clean(value: Any) -> str | None:
    """单元格 → 去空白的非空字符串；None / NaN / 空串归一成 None。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, node: str) -> str:
        self.parent.setdefault(node, node)
        if self.parent[node] != node:
            self.parent[node] = self.find(self.parent[node])
        return self.parent[node]

    def union(self, left: str, right: str) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left

    def clusters(self) -> tuple[tuple[str, ...], ...]:
        grouped: dict[str, list[str]] = {}
        for node in self.parent:
            grouped.setdefault(self.find(node), []).append(node)
        groups = [tuple(sorted(tables)) for tables in grouped.values()]
        return tuple(sorted(groups, key=lambda tables: tables[0]))


class DomainHandler(TaskHandler):
    """产出 :class:`DomainSnapshot`（domains + members 两张关系表）。"""

    def __init__(
        self,
        llm: AsyncLlmClient | None,
        *,
        temperature: float = 0.2,
        language: str = "zh",
        mode: str = MODE_CREATE,
    ) -> None:
        if mode not in (MODE_CREATE, MODE_REFINE):
            raise ValueError(f"未知 handler 模式 {mode!r}，可选: create / refine")
        self._language = language
        self._llm = llm
        self._temperature = temperature
        self._mode = mode

    # -- 输入装配 --------------------------------------------------------

    @staticmethod
    def _table_comments(evidence: TaskEvidence) -> dict[str, str]:
        comments: dict[str, str] = {}
        tables = evidence.tables
        if tables is None or tables.is_empty():
            return comments
        meta = tables.table_meta
        has_comment = "table_comment" in meta.columns
        for row in meta.itertuples(index=False):
            name = str(getattr(row, "table_name", "") or "").strip()
            if not name:
                continue
            raw = getattr(row, "table_comment", None) if has_comment else None
            comments[name] = (
                ""
                if raw is None or (isinstance(raw, float) and pd.isna(raw))
                else str(raw).strip()
            )
        return comments

    @staticmethod
    def _clusters(join_frame: pd.DataFrame) -> tuple[tuple[str, ...], ...]:
        union_find = _UnionFind()
        for row in join_frame.itertuples(index=False):
            union_find.union(str(row.left_table), str(row.right_table))
        return union_find.clusters()

    @staticmethod
    def _build_request(
        clusters: tuple[tuple[str, ...], ...],
        join_frame: pd.DataFrame,
        expected_tables: frozenset[str],
        table_comments: dict[str, str],
    ) -> dict[str, Any]:
        """create：cluster 与未聚类表的结构化 Prompt 输入（形状同 legacy）。"""
        cluster_items: list[dict[str, Any]] = []
        for index, tables in enumerate(clusters):
            table_set = set(tables)
            sample_joins = [
                {
                    "left": row.left_table,
                    "right": row.right_table,
                    "on": row.on_columns,
                    "count": int(row.count),
                }
                for row in join_frame.itertuples(index=False)
                if row.left_table in table_set and row.right_table in table_set
            ]
            cluster_items.append(
                {
                    "cluster_id": f"cluster_{index}",
                    "tables": [
                        {
                            "table_name": table,
                            "table_comment": table_comments.get(table, ""),
                        }
                        for table in tables
                    ],
                    "cluster_size": len(tables),
                    "sample_joins": sample_joins[:10],
                }
            )

        clustered = {table for tables in clusters for table in tables}
        return {
            "cluster_items": cluster_items,
            "unclustered_items": [
                {
                    "table_name": table,
                    "table_comment": table_comments.get(table, ""),
                }
                for table in sorted(expected_tables)
                if table not in clustered
            ],
        }

    @staticmethod
    def _build_refine_request(
        drafts: list[dict[str, Any]],
        table_comments: dict[str, str],
        *,
        free_tables: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """refine：待完善草稿（成员表附注释）+ 可供零成员草稿分配的空闲表。"""
        payload: dict[str, Any] = {
            "draft_domains": [
                {
                    "draft_id": draft["draft_id"],
                    "domain": draft["domain"],
                    "display_name": draft["display_name"] or "",
                    "description": draft["description"] or "",
                    "tables": [
                        {
                            "table_name": table,
                            "table_comment": table_comments.get(table, ""),
                        }
                        for table in draft["tables"]
                    ],
                }
                for draft in drafts
            ]
        }
        if free_tables:
            payload["unassigned_tables"] = [
                {
                    "table_name": table,
                    "table_comment": table_comments.get(table, ""),
                }
                for table in sorted(free_tables)
            ]
        return payload

    # -- 响应解析（全批 all-or-nothing，非法即抛，由重试兜底）------------

    @staticmethod
    def _catalog_name(
        value: Any,
        catalog: dict[str, dict[str, Any]] | frozenset[str] | set[str],
    ) -> str | None:
        """将模型回传的名称锚定到已有 Domain 的精确自然键。

        精确匹配优先；大小写不同时仅在 casefold 命中唯一时自动
        归一。若历史数据本身有大小写冲突，宁可不猜，交给新名
        校验报错。
        """
        name = _clean(value)
        if not name:
            return None
        names = catalog.keys() if isinstance(catalog, dict) else catalog
        if name in names:
            return name
        folded = [
            candidate for candidate in names if candidate.casefold() == name.casefold()
        ]
        return folded[0] if len(folded) == 1 else None

    @classmethod
    def _canonicalize_name(
        cls,
        value: Any,
        catalog: dict[str, dict[str, Any]] | frozenset[str] | set[str],
    ) -> tuple[str, bool]:
        """返回最终名称及其是否来自 catalog。

        所有入口（包括 refine passthrough）都必须走同一规则：先锚定
        既有名称；否则按新 Domain 处理，lowercase 后校验标识格式。
        """
        canonical = cls._catalog_name(value, catalog)
        if canonical is not None:
            return canonical, True
        raw_name = _clean(value) or ""
        name = raw_name.lower()
        if not _DOMAIN_NAME_RE.fullmatch(name):
            raise ValueError(f"非法 domain 名称: {raw_name!r}")
        return name, False

    @staticmethod
    def _parse_assignments(
        response: str,
        expected_tables: frozenset[str],
        clusters: tuple[tuple[str, ...], ...] = (),
        *,
        refine: bool = False,
        catalog: dict[str, dict[str, Any]] | frozenset[str] | set[str] = frozenset(),
        existing_only: bool = False,
    ) -> list[dict[str, Any]]:
        """校验名称与唯一表归属；create 另校验非空及 cluster 一致。

        refine 的成员最终仍由 ``_apply_refine`` 按草稿规则恢复，但模型响应中
        同一表指向不同 Domain 也是整批冲突，进入既有重试/回退路径。
        """
        payload = extract_json_payload(response)
        if not isinstance(payload, dict):
            raise ValueError("LLM 输出必须是 JSON 对象")
        raw_domains = payload.get("domains")
        if not isinstance(raw_domains, list):
            raise ValueError("domains 必须是数组")

        assignments: list[dict[str, Any]] = []
        table_to_domain: dict[str, str] = {}
        for item in raw_domains:
            if not isinstance(item, dict):
                raise ValueError("domains 数组元素必须是对象")
            domain, reused = DomainHandler._canonicalize_name(
                item.get("domain"), catalog
            )
            if existing_only and not reused:
                raise ValueError(
                    "Dataset 归属只能选择正式 Domain，无法唯一匹配 "
                    f"{item.get('domain')!r}（不能新增或猜测）"
                )
            draft_id: str | None = None
            if refine:
                draft_id = str(item.get("draft_id", "") or "").strip()
                if not draft_id:
                    raise ValueError(f"domain {domain} 缺少 draft_id")

            raw_tables = item.get("tables")
            if not isinstance(raw_tables, list):
                raw_tables = [] if refine else None
            if raw_tables is None:
                raise ValueError(f"domain {domain} 缺少 tables 数组")
            tables: list[str] = []
            for raw in raw_tables:
                table = str(raw or "").strip()
                if not table:
                    continue
                if table not in expected_tables:
                    # 模型编造 / 改写的表名不计入，不因此整批作废
                    logger.info("domain %s: 忽略未知表 %r", domain, table)
                    continue
                if table in table_to_domain and table_to_domain[table] != domain:
                    raise ValueError(
                        f"表 {table} 被映射到多个 domain: "
                        f"{table_to_domain[table]} 与 {domain}"
                    )
                table_to_domain[table] = domain
                tables.append(table)
            if not tables and not refine:
                raise ValueError(f"domain {domain} 没有任何有效成员表")

            assignments.append(
                {
                    "domain": domain,
                    "display_name": _clean(item.get("display_name")) or "",
                    "description": _clean(item.get("description")) or "",
                    "tables": sorted(set(tables)),
                    "draft_id": draft_id,
                }
            )

        if not refine:
            # cluster 一致性：同一 cluster 的表必须同 domain（忠实 legacy 硬校验）
            for tables in clusters:
                assigned = {
                    table_to_domain[table]
                    for table in tables
                    if table in table_to_domain
                }
                if len(assigned) > 1:
                    raise ValueError(
                        f"同一 cluster 被映射到多个 domain: {list(tables)} -> {sorted(assigned)}"
                    )
        return assignments

    @staticmethod
    def _as_snapshot(assignments: list[dict[str, Any]], origin: str) -> DomainSnapshot:
        domain_rows: list[dict[str, Any]] = []
        member_rows: list[dict[str, Any]] = []
        for domain_id, assignment in enumerate(assignments):
            domain_rows.append(
                {
                    "domain_id": domain_id,
                    "domain_name": assignment["domain"],
                    "display_name": assignment["display_name"] or None,
                    "description": assignment["description"] or None,
                    # 额外列：本轮是新增还是完善，投影层带给 CandidateBizDomain.origin
                    "origin": origin,
                }
            )
            member_rows.extend(
                {
                    "table_name": table,
                    "domain_id": domain_id,
                    "domain_name": assignment["domain"],
                }
                for table in assignment["tables"]
            )
        return DomainSnapshot.build(
            pd.DataFrame(domain_rows), pd.DataFrame(member_rows)
        )

    # -- 既有状态（prior）：可复用目录、锁定表与待完善草稿 -------------

    @staticmethod
    def _prior_entries(
        domains_frame: pd.DataFrame | None,
        members_frame: pd.DataFrame | None,
    ) -> list[dict[str, Any]]:
        """domains ⋈ members → 每个 Domain 行一个条目。

        confirmed 侧的成员来自正式 dataset 行，可能引用 domains 里没有的
        域名（域行被过滤等）—— 为这类「只有成员」的域合成占位条目，
        归属信息不能丢。

        不能按 ``domain_name`` 去重：staging 允许同名的 CONFIRMED
        域行与 PENDING 增量行并存。按名归并会把后者的状态和成员
        吞进前者，refine 就再也看不到待完善草稿。
        """
        members_by_entry: dict[tuple[str | None, str], list[str]] = {}
        member_status_by_entry: dict[tuple[str | None, str], set[str]] = {}
        domains_have_status = (
            domains_frame is not None and "probe_status" in domains_frame.columns
        )
        members_have_status = (
            members_frame is not None and "probe_status" in members_frame.columns
        )
        if (
            domains_frame is not None
            and members_frame is not None
            and len(domains_frame) > 0
            and len(members_frame) > 0
            and domains_have_status != members_have_status
        ):
            raise ValueError(
                "Domain staging 生命周期列不一致："
                "domains 与 members 必须同时带 probe_status，或同时不带"
            )
        if members_frame is not None and len(members_frame) > 0:
            for row in members_frame.itertuples(index=False):
                name = _clean(getattr(row, "domain_name", None))
                table = _clean(getattr(row, "table_name", None))
                if name and table:
                    domain_id = _clean(getattr(row, "domain_id", None))
                    key = (domain_id, name)
                    members_by_entry.setdefault(key, []).append(table)
                    if members_have_status:
                        status = _clean(getattr(row, "probe_status", None))
                        if status is None:
                            raise ValueError(
                                "Domain staging 成员缺少 probe_status："
                                f"table={table!r}, domain={name!r}"
                            )
                        member_status_by_entry.setdefault(key, set()).add(
                            status.upper()
                        )

        entries: list[dict[str, Any]] = []
        entry_by_key: dict[tuple[str | None, str], dict[str, Any]] = {}
        if domains_frame is not None and len(domains_frame) > 0:
            has_source = "source_type" in domains_frame.columns
            for row in domains_frame.itertuples(index=False):
                name = _clean(getattr(row, "domain_name", None))
                if not name:
                    continue
                domain_id = _clean(getattr(row, "domain_id", None))
                key = (domain_id, name)
                # domain_id 是正式 id / staging row id / 算法局部序号，
                # 正常路径唯一。只有丢 id 的旧快照才会回落到名。
                status = (
                    _clean(getattr(row, "probe_status", None))
                    if domains_have_status
                    else None
                )
                if domains_have_status and status is None:
                    raise ValueError(
                        "Domain staging 行缺少 probe_status："
                        f"id={domain_id!r}, domain={name!r}"
                    )
                member_statuses = member_status_by_entry.get(key, set())
                if len(member_statuses) > 1 or (
                    member_statuses
                    and status is not None
                    and member_statuses != {status.upper()}
                ):
                    raise ValueError(
                        "Domain staging 行与成员状态冲突："
                        f"id={domain_id!r}, domain={name!r}, "
                        f"row={status!r}, members={sorted(member_statuses)!r}"
                    )
                entry = {
                    "domain_id": domain_id,
                    "domain": name,
                    "display_name": _clean(getattr(row, "display_name", None)),
                    "description": _clean(getattr(row, "description", None)),
                    "tables": sorted(set(members_by_entry.get(key, []))),
                    "probe_status": status,
                    "source_type": (
                        _clean(getattr(row, "source_type", None))
                        if has_source
                        else None
                    ),
                }
                previous = entry_by_key.get(key)
                if previous is not None:
                    if domain_id is None:
                        raise ValueError(
                            "Domain staging 行身份不明确："
                            f"同名 {name!r} 的多行缺少 domain_id，无法关联成员"
                        )
                    if entry != previous:
                        raise ValueError(
                            "Domain 行身份冲突："
                            f"id={domain_id!r}, domain={name!r} 对应多份不同定义"
                        )
                    logger.warning(
                        "domain prior: 忽略完全重复行 id=%r name=%s",
                        domain_id,
                        name,
                    )
                    continue
                entry_by_key[key] = entry
                entries.append(entry)
        for (domain_id, name), tables in members_by_entry.items():
            if (domain_id, name) not in entry_by_key:
                if members_have_status:
                    raise ValueError(
                        "Domain staging 成员缺少对应 Domain 行："
                        f"id={domain_id!r}, domain={name!r}"
                    )
                entries.append(
                    {
                        "domain_id": domain_id,
                        "domain": name,
                        "display_name": None,
                        "description": None,
                        "tables": sorted(set(tables)),
                        "probe_status": None,
                        "source_type": None,
                    }
                )
        return entries

    @classmethod
    def _prior_state(cls, evidence: TaskEvidence) -> _PriorState:
        """构造「可复用目录」与「当前源已确认表归属」。

        formal 是全局 canonical truth；staging CONFIRMED 仅在 formal 尚不存在
        时补充定义，但它的当前源成员一样进锁定表。PENDING / SKIPPED
        不进任何一侧。
        """
        catalog: dict[str, dict[str, Any]] = {}
        locked_table_owner: dict[str, str] = {}

        def add_catalog(entry: dict[str, Any], *, formal: bool) -> None:
            name = entry["domain"]
            previous = catalog.get(name)
            if previous is None:
                catalog[name] = {**entry, "tables": list(entry["tables"])}
                return
            if formal:
                # 历史重名 formal 理论上应由 workspace 唯一约束阻止。
                # 此处不猜 id，保留先到 canonical 并暴露风险。
                logger.warning(
                    "domain prior: 检测到重名 formal Domain %s，保留首条", name
                )
            # formal 目录先装入；staging CONFIRMED 只能补充尚未
            # 正式落库的 Domain，不得给已有定义补写或覆盖元信息。

        def lock_tables(entry: dict[str, Any], *, source: str) -> None:
            name = entry["domain"]
            for table in entry["tables"]:
                owner = locked_table_owner.get(table)
                if owner is None:
                    locked_table_owner[table] = name
                elif owner != name:
                    raise ValueError(
                        "Domain 归属冲突："
                        f"表 {table!r} 已属于 {owner!r}，"
                        f"但 {source} 又将其归入 {name!r}"
                    )

        confirmed = evidence.prior_confirmed
        if confirmed is not None:
            frames = (
                getattr(confirmed, "domains", None),
                getattr(confirmed, "members", None),
            )
            for entry in cls._prior_entries(*frames):
                add_catalog(entry, formal=True)
                lock_tables(entry, source="formal")
        staged = evidence.prior_staged
        if staged is not None:
            frames = (
                getattr(staged, "domains", None),
                getattr(staged, "members", None),
            )
            for entry in cls._prior_entries(*frames):
                status = (entry.get("probe_status") or "").upper()
                if status != _STATUS_CONFIRMED:
                    continue
                add_catalog(entry, formal=False)
                lock_tables(entry, source="staging CONFIRMED")

        return _PriorState(catalog=catalog, locked_table_owner=locked_table_owner)

    @classmethod
    def _draft_domains(
        cls,
        evidence: TaskEvidence,
        prior: _PriorState,
        all_tables: frozenset[str],
    ) -> list[dict[str, Any]]:
        """refine 的输入集合：staging 中全部 PENDING 草稿（ALGO / HUMAN，含零成员）。

        状态列缺失（算法层直调回读本地快照）视同 PENDING；SKIPPED 不是输入
        （skip = 删除）。成员已不在源表或已被锁定域占用的表剔除；剔空后仍保留
        草稿（零成员可在 refine 中从未分配表补成员 / 补文案）。
        """
        staged = evidence.prior_staged
        if staged is None:
            return []
        locked_tables = set(prior.locked_table_owner)
        frames = (getattr(staged, "domains", None), getattr(staged, "members", None))
        drafts: list[dict[str, Any]] = []
        claimed_pending: dict[str, str] = {}
        for entry in cls._prior_entries(*frames):
            status = (entry.get("probe_status") or _STATUS_PENDING).upper()
            if status != _STATUS_PENDING:
                continue
            tables: list[str] = []
            for table in entry["tables"]:
                if table not in all_tables or table in locked_tables:
                    continue
                previous = claimed_pending.get(table)
                if previous is not None and previous != entry["domain"]:
                    raise ValueError(
                        "Domain 草稿归属冲突："
                        f"表 {table!r} 同时出现在 "
                        f"{previous!r} 与 {entry['domain']!r}"
                    )
                if previous is not None:
                    continue
                claimed_pending[table] = entry["domain"]
                tables.append(table)
            drafts.append(
                {**entry, "tables": sorted(tables), "draft_id": f"draft_{len(drafts)}"}
            )
        return drafts

    @staticmethod
    def _context_payload(prior: _PriorState) -> list[dict[str, Any]] | None:
        """可复用 Domain 目录 → prompt 上下文 JSON。"""
        if not prior.catalog:
            return None
        payload = []
        for name in sorted(prior.catalog):
            entry = prior.catalog[name]
            item: dict[str, Any] = {"domain": entry["domain"]}
            if entry.get("display_name"):
                item["display_name"] = entry["display_name"]
            if entry.get("description"):
                item["description"] = entry["description"]
            payload.append(item)
        return payload

    # -- 确定性兜底（不依赖 prompt 被遵守）--------------------------------

    @classmethod
    def _apply_prior(
        cls,
        assignments: list[dict[str, Any]],
        prior: _PriorState,
        *,
        drop_empty: bool = True,
    ) -> list[dict[str, Any]]:
        """应用 prior 确定性约束：锚定已有名与元信息，剥离已锁表。

        Domain 在 catalog 中不再是删除理由；它只表示本轮要复用该
        canonical 定义。已确认成员不重新产出，新成员仍可挂到同一
        Domain。refine 传 ``drop_empty=False`` 保留零成员草稿。
        """
        locked_tables = set(prior.locked_table_owner)
        out: list[dict[str, Any]] = []
        for assignment in assignments:
            name, reused = cls._canonicalize_name(assignment["domain"], prior.catalog)
            kept = [t for t in assignment["tables"] if t not in locked_tables]
            stripped = set(assignment["tables"]) - set(kept)
            if stripped:
                owners = {
                    table: prior.locked_table_owner[table] for table in sorted(stripped)
                }
                logger.warning(
                    "domain: 已锁定成员 %s 不重新产出到 %s，已剥离",
                    owners,
                    name,
                )
            if not kept and drop_empty:
                continue
            normalized = {
                **assignment,
                "domain": name,
                "tables": sorted(set(kept)),
            }
            if reused:
                entry = prior.catalog[name]
                # 已有 Domain 的元信息是 truth。不用模型文案伪装成一次
                # 全局更新：promote 对已有名本来就会 skip。
                normalized["display_name"] = entry.get("display_name") or ""
                normalized["description"] = entry.get("description") or ""
            out.append(normalized)
        return out

    @classmethod
    def _coalesce_assignments(
        cls,
        assignments: list[dict[str, Any]],
        prior: _PriorState,
    ) -> list[dict[str, Any]]:
        """按 canonical Domain 合并多个 cluster / draft，保证快照名唯一。"""
        grouped: dict[str, dict[str, Any]] = {}
        table_owner: dict[str, str] = {}
        for assignment in assignments:
            name, reused = cls._canonicalize_name(assignment["domain"], prior.catalog)
            current = grouped.get(name)
            if current is None:
                current = {
                    "domain": name,
                    "display_name": assignment.get("display_name") or "",
                    "description": assignment.get("description") or "",
                    "tables": [],
                }
                grouped[name] = current
            elif not reused:
                # 新域同名时保留首个非空文案，成员仍汇总。
                current["display_name"] = (
                    current["display_name"] or assignment.get("display_name") or ""
                )
                current["description"] = (
                    current["description"] or assignment.get("description") or ""
                )

            for table in assignment.get("tables") or []:
                owner = table_owner.get(table)
                if owner is not None and owner != name:
                    raise ValueError(
                        "Domain 产出归属冲突："
                        f"表 {table!r} 同时归入 {owner!r} 与 {name!r}"
                    )
                table_owner[table] = name
                current["tables"].append(table)

        for name, assignment in grouped.items():
            assignment["tables"] = sorted(set(assignment["tables"]))
            if name in prior.catalog:
                entry = prior.catalog[name]
                assignment["display_name"] = entry.get("display_name") or ""
                assignment["description"] = entry.get("description") or ""
        return list(grouped.values())

    @classmethod
    def _apply_refine(
        cls,
        assignments: list[dict[str, Any]],
        drafts: list[dict[str, Any]],
        prior: _PriorState,
        *,
        free_tables: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """refine 附加规则：按 draft_id 对齐；已有成员强制恢复；零成员从未分配表
        接受 LLM 分配（先到先得）；未回带的草稿保留后进入统一收口。
        多个草稿命中同一 canonical 名称时由后续步骤合并。"""
        draft_ids = {draft["draft_id"] for draft in drafts}
        by_draft: dict[str, dict[str, Any]] = {}
        for assignment in assignments:
            draft_id = assignment.get("draft_id")
            if draft_id not in draft_ids or draft_id in by_draft:
                logger.warning(
                    "domain: refine 产出的 draft_id %r 未知或重复，丢弃", draft_id
                )
                continue
            by_draft[draft_id] = assignment

        free = set(free_tables)
        out: list[dict[str, Any]] = []
        for draft in drafts:
            got = by_draft.get(draft["draft_id"])
            if got is None:
                logger.warning(
                    "domain: refine 遗漏草稿 %s，保留并进入确定性收口",
                    draft["domain"],
                )
                got = {
                    "domain": draft["domain"],
                    "display_name": draft["display_name"] or "",
                    "description": draft["description"] or "",
                    "tables": list(draft["tables"]),
                }
            name, _ = cls._canonicalize_name(got["domain"], prior.catalog)
            if draft["tables"]:
                tables = list(draft["tables"])
            else:
                claimed: list[str] = []
                for table in got.get("tables") or []:
                    if table in free:
                        claimed.append(table)
                        free.discard(table)
                tables = sorted(set(claimed))
            out.append(
                {
                    "domain": name,
                    "display_name": got["display_name"],
                    "description": got["description"],
                    "tables": tables,
                }
            )
        return out

    # -- 主流程 ----------------------------------------------------------

    async def assign_existing(
        self,
        evidence: TaskEvidence,
        *,
        runner: LlmRunner | None = None,
    ) -> DomainSnapshot:
        """Dataset 内部补分配：复用 create 的聚类与校验，但禁止新增 Domain。

        输入成员已经由 evidence 过滤为当前源有效确认；没有归属的表均可
        重新提议，包括 SKIPPED Domain 的成员。已确认归属与本次补分配合计
        覆盖全部输入表的比例必须大于 80%；已有归属已达标时不调用 LLM。
        返回值只供 Dataset 在内存中消费，不调用 DOMAIN 任务，也不写
        Domain context / staging。
        """
        table_comments = self._table_comments(evidence)
        all_tables = frozenset(table_comments)
        locked: dict[str, str] = {}
        if evidence.domains is not None:
            for table in all_tables:
                owner = evidence.domains.domain_of(table)
                if owner:
                    locked[table] = owner
        missing = sorted(all_tables - locked.keys())
        if not missing:
            return DomainSnapshot.build(None, None)
        if len(locked) / len(all_tables) > MIN_DOMAIN_COVERAGE:
            logger.info(
                "dataset: 已确认归属总体覆盖率=%.2f%%，无需补分配",
                len(locked) / len(all_tables) * 100,
            )
            return DomainSnapshot.build(None, None)

        catalog = {
            entry["domain"]: entry
            for entry in self._prior_entries(evidence.domain_catalog, None)
        }
        if not catalog:
            raise ValueError(
                f"Dataset 待归属表 {missing!r} 没有可选的正式 Domain；"
                "请先创建并完成 Domain 建模，再运行 Dataset"
            )
        if self._llm is None:
            raise ValueError(
                f"Dataset 待归属表 {missing!r} 需要 LLM 完成正式 Domain 分配"
            )
        return await self._create(
            evidence,
            table_comments,
            all_tables,
            _PriorState(catalog, locked),
            existing_only=True,
            runner=runner,
        )

    async def handle(self, evidence: TaskEvidence) -> DomainSnapshot:
        table_comments = self._table_comments(evidence)
        all_tables = frozenset(table_comments)
        prior = self._prior_state(evidence)
        if self._mode == MODE_REFINE:
            return await self._refine(evidence, table_comments, all_tables, prior)
        return await self._create(evidence, table_comments, all_tables, prior)

    async def _create(
        self,
        evidence: TaskEvidence,
        table_comments: dict[str, str],
        all_tables: frozenset[str],
        prior: _PriorState,
        *,
        existing_only: bool = False,
        runner: LlmRunner | None = None,
    ) -> DomainSnapshot:
        locked_tables = set(prior.locked_table_owner)
        expected_tables = frozenset(all_tables - locked_tables)
        if not expected_tables:
            logger.info(
                "domain[create]: 无待划分的表（源表 %d，已锁定 %d），产出空快照",
                len(all_tables),
                len(locked_tables & all_tables),
            )
            return DomainSnapshot.build(None, None)

        def assignment_coverage(snapshot: DomainSnapshot) -> float:
            if existing_only:
                covered = (locked_tables | snapshot.tables()) & all_tables
                return len(covered) / len(all_tables)
            return snapshot.coverage(expected_tables)

        def meets_threshold(coverage: float) -> bool:
            if existing_only:
                return coverage > MIN_DOMAIN_COVERAGE
            return coverage >= MIN_DOMAIN_COVERAGE

        if self._llm is None:
            logger.warning("domain[create]: 未配置 LLM，产出空快照（D4 降级）")
            return DomainSnapshot.build(None, None)

        join_frame = (
            evidence.logs.of("join")
            if evidence.has_log("join")
            else pd.DataFrame(
                columns=["left_table", "right_table", "on_columns", "count"]
            )
        )
        if len(join_frame):
            # 聚类只在未锁定表上做：一端落在锁定域里的边不再是本轮证据
            join_frame = join_frame[
                join_frame["left_table"].astype(str).isin(expected_tables)
                & join_frame["right_table"].astype(str).isin(expected_tables)
            ]
        clusters = self._clusters(join_frame)
        request = self._build_request(
            clusters, join_frame, expected_tables, table_comments
        )
        context = self._context_payload(prior)
        system_prompt = build_domain_system_prompt(
            self._language,
            mode=MODE_CREATE,
            existing_only=existing_only,
        )

        async def complete(user_prompt: str) -> str:
            # Dataset 使用既有 runner，保持配置温度及每次调用前的取消检查；
            # 普通 DOMAIN create 仍走原来的直接调用路径。
            if runner is not None:
                return await runner.run_one(
                    LlmUnit("dataset:domain-assignment", system_prompt, user_prompt)
                )
            return await self._llm.complete(
                system_prompt,
                user_prompt,
                temperature=self._temperature,
            )

        logger.info(
            "LLM call: domain[create] tables=%d (locked %d) clusters=%d",
            len(expected_tables),
            len(locked_tables),
            len(clusters),
        )
        initial_response = await complete(
            build_domain_user_prompt(
                request,
                self._language,
                context=context,
                mode=MODE_CREATE,
                existing_only=existing_only,
            ),
        )

        previous_error: str | None = None
        try:
            assignments = self._parse_assignments(
                initial_response,
                expected_tables,
                clusters,
                catalog=prior.catalog,
                existing_only=existing_only,
            )
        except Exception as exc:
            previous_error = str(exc)
            logger.warning("Invalid initial domain response: %s", exc)
            assignments = []

        initial_assignments = self._coalesce_assignments(
            self._apply_prior(assignments, prior), prior
        )
        initial_snapshot = self._as_snapshot(initial_assignments, MODE_CREATE)
        coverage = assignment_coverage(initial_snapshot)
        if meets_threshold(coverage):
            logger.info(
                "domain[create]: initial coverage=%.2f%%，无需重试", coverage * 100
            )
            return initial_snapshot

        unmapped = sorted(expected_tables - initial_snapshot.tables())
        logger.info(
            "LLM call: domain[create] retry coverage=%.2f%% error=%s",
            coverage * 100,
            previous_error or "(coverage)",
        )
        retry_response = await complete(
            build_domain_retry_prompt(
                request,
                unmapped,
                initial_response,
                previous_error=previous_error,
                language=self._language,
                context=context,
                mode=MODE_CREATE,
                existing_only=existing_only,
            ),
        )
        try:
            retried_assignments = self._parse_assignments(
                retry_response,
                expected_tables,
                clusters,
                catalog=prior.catalog,
                existing_only=existing_only,
            )
        except Exception as exc:
            previous_error = str(exc)
            logger.warning("Invalid retried domain response: %s", exc)
            retried_assignments = []
        retried_assignments = self._coalesce_assignments(
            self._apply_prior(retried_assignments, prior), prior
        )
        retried = self._as_snapshot(retried_assignments, MODE_CREATE)

        chosen = (
            retried if assignment_coverage(retried) >= coverage else initial_snapshot
        )
        final_coverage = assignment_coverage(chosen)
        if existing_only and not meets_threshold(final_coverage):
            missing = sorted(expected_tables - chosen.tables())
            detail = f"；输出校验错误：{previous_error}" if previous_error else ""
            raise ValueError(
                f"Dataset 仍有表未能分配到正式 Domain：{missing!r}{detail}；"
                f"总体覆盖率 {final_coverage:.1%} 未超过 {MIN_DOMAIN_COVERAGE:.0%}；"
                "请检查或补充正式 Domain 后重试，本轮不保存部分 Dataset 候选"
            )
        if final_coverage < MIN_DOMAIN_COVERAGE:
            logger.warning(
                "Domain coverage remains below threshold after retry: %.2f%% < %.2f%%",
                final_coverage * 100,
                MIN_DOMAIN_COVERAGE * 100,
            )
        return chosen

    async def _refine(
        self,
        evidence: TaskEvidence,
        table_comments: dict[str, str],
        all_tables: frozenset[str],
        prior: _PriorState,
    ) -> DomainSnapshot:
        drafts = self._draft_domains(evidence, prior, all_tables)
        if not drafts:
            logger.info("domain[refine]: 无待完善的草稿，产出空快照")
            return DomainSnapshot.build(None, None)

        locked_tables = set(prior.locked_table_owner)
        owned = frozenset(t for d in drafts for t in d["tables"])
        free_tables = frozenset(all_tables - locked_tables - owned)

        def passthrough() -> DomainSnapshot:
            assignments = [
                {
                    "domain": d["domain"],
                    "display_name": d["display_name"] or "",
                    "description": d["description"] or "",
                    "tables": list(d["tables"]),
                }
                for d in drafts
            ]
            assignments = self._coalesce_assignments(
                self._apply_prior(assignments, prior, drop_empty=False), prior
            )
            return self._as_snapshot(assignments, MODE_REFINE)

        if self._llm is None:
            logger.warning(
                "domain[refine]: 未配置 LLM，草稿经确定性收口产出（D4 降级）"
            )
            return passthrough()

        expected_tables = owned | free_tables
        request = self._build_refine_request(
            drafts, table_comments, free_tables=free_tables
        )
        context = self._context_payload(prior)
        system_prompt = build_domain_system_prompt(self._language, mode=MODE_REFINE)

        logger.info(
            "LLM call: domain[refine] drafts=%d (locked %d, free_tables %d)",
            len(drafts),
            len(locked_tables),
            len(free_tables),
        )
        response = await self._llm.complete(
            system_prompt,
            build_domain_user_prompt(
                request, self._language, context=context, mode=MODE_REFINE
            ),
            temperature=self._temperature,
        )
        try:
            assignments = self._parse_assignments(
                response, expected_tables, refine=True, catalog=prior.catalog
            )
        except Exception as exc:
            logger.warning("Invalid refine response: %s —— 带报错重试一次", exc)
            retry_response = await self._llm.complete(
                system_prompt,
                build_domain_retry_prompt(
                    request,
                    [d["draft_id"] for d in drafts],
                    response,
                    previous_error=str(exc),
                    language=self._language,
                    context=context,
                    mode=MODE_REFINE,
                ),
                temperature=self._temperature,
            )
            try:
                assignments = self._parse_assignments(
                    retry_response, expected_tables, refine=True, catalog=prior.catalog
                )
            except Exception as exc2:
                logger.warning(
                    "Invalid retried refine response: %s —— 草稿经确定性收口产出",
                    exc2,
                )
                return passthrough()

        merged = self._apply_refine(assignments, drafts, prior, free_tables=free_tables)
        merged = self._apply_prior(merged, prior, drop_empty=False)
        merged = self._coalesce_assignments(merged, prior)
        return self._as_snapshot(merged, MODE_REFINE)


__all__ = ["MIN_DOMAIN_COVERAGE", "MODE_CREATE", "MODE_REFINE", "DomainHandler"]
