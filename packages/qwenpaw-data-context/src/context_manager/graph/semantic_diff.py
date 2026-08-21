"""三方向 diff + 增量应用（spec §5）。

- B = baseline（导出时刻的 key 清单，来自 ``_meta``）
- F = file（用户编辑后的 xlsx 解析结果）
- G = graph（回灌时图当前状态）

规则：
- 新增/更新 = F vs G
- 删除 = key ∈ B 且 key ∉ F 且 key 仍 ∈ G
- 引用计数守卫：共享节点（Dataset/DatasetColumn）被引用时进 conflict
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import (
    dataset_column_key,
    dataset_key,
    dim_key,
    dim_value_key,
    domain_key,
    formula_key,
    metric_key,
)
from .semantic_template_io import (
    ExportBaseline,
    ParsedImport,
    SHEET_ORDER,
    build_bundle_from_graph,
    _compute_graph_digest,
    _collect_entity_keys,
)

log = get_logger("graph.semantic_diff")


# ---------------------------------------------------------------------- #
# Change 记录
# ---------------------------------------------------------------------- #

@dataclass
class Change:
    type: str          # sheet 名（dataset / metric / ...）
    key: str           # row_key（$$$ 拼接）
    op: str            # "add" / "update" / "delete" / "conflict"
    fields: dict[str, list] = field(default_factory=dict)  # update: {col: [old,new]}
    reason: str = ""   # delete/conflict 说明
    row: Optional[dict] = None  # add/update: 目标行（F 中的行，用于写入）


@dataclass
class DiffResult:
    plan_id: str
    datasource_id: str
    graph_digest: str  # confirm 时重算比对（漂移守卫）
    changes: list[Change]
    summary: dict[str, int]

    def by_op(self, op: str) -> list[Change]:
        return [c for c in self.changes if c.op == op]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "datasource_id": self.datasource_id,
            "graph_digest": self.graph_digest,
            "changes": [
                {
                    "type": change.type,
                    "key": change.key,
                    "op": change.op,
                    "fields": change.fields,
                    "reason": change.reason,
                    "row": change.row,
                }
                for change in self.changes
            ],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiffResult":
        return cls(
            plan_id=str(data["plan_id"]),
            datasource_id=str(data["datasource_id"]),
            graph_digest=str(data["graph_digest"]),
            changes=[Change(**item) for item in data.get("changes", [])],
            summary={
                str(key): int(value)
                for key, value in dict(data.get("summary", {})).items()
            },
        )


# ---------------------------------------------------------------------- #
# 三方向 diff
# ---------------------------------------------------------------------- #

def _row_field_diff(sheet: str, f_row: dict, g_row: dict) -> dict[str, list]:
    """比较一行内各字段差异，返回 {col: [old, new]}。"""
    from .semantic_template_io import MULTIVALUE_COLUMNS, SHEET_COLUMNS
    diff: dict[str, list] = {}
    for col in SHEET_COLUMNS.get(sheet, []):
        fv = f_row.get(col)
        gv = g_row.get(col)
        if col in MULTIVALUE_COLUMNS:
            fl = sorted(v for v in (fv or []) if v)
            gl = sorted(v for v in (gv or []) if v)
            if fl != gl:
                diff[col] = [gl, fl]
        else:
            fs = str(fv).strip() if fv is not None else ""
            gs = str(gv).strip() if gv is not None else ""
            if fs != gs:
                diff[col] = [gs, fs]
    return diff


def compute_diff(
    driver: Driver,
    parsed: ParsedImport,
) -> DiffResult:
    """算三方向 diff。不写图。"""
    ds = (parsed.datasource_id or "").strip()
    if not ds:
        raise ValueError("datasource_id is empty in uploaded file")

    # 校验数据源是否已注册
    from .datasource_registry import try_resolve
    ds_info = try_resolve(ds)
    if ds_info is None:
        from .datasource_registry import list_datasources
        known = sorted(d.datasource_id for d in list_datasources())
        raise ValueError(
            f"unknown datasource_id '{ds}'; "
            f"known datasources: {known}"
        )

    # 校验图中是否已有该数据源的语义层节点
    with neo4j_session(driver) as s:
        result = s.run(
            "MATCH (n) "
            "WHERE (n:Domain OR n:Dataset OR n:Metric OR n:Dimension) "
            "  AND n.datasource_id = $ds "
            "RETURN count(n) > 0 AS has_nodes",
            ds=ds,
        ).single()
        graph_has_nodes = result["has_nodes"] if result else False

    if not graph_has_nodes and not parsed.meta:
        log.warning(
            "datasource %r has no semantic nodes in graph and xlsx has no _meta baseline; "
            "all rows will be treated as additions",
            ds,
        )

    # G = 当前图（重新 build bundle 取行 + keys）
    g_bundle = build_bundle_from_graph(driver, ds)
    g_rows: dict[str, list[dict]] = g_bundle["rows_by_sheet"]
    g_keys: dict[str, list[str]] = g_bundle["meta"].entity_keys

    # B = baseline
    b_keys: dict[str, list[str]] = (
        parsed.meta.entity_keys if parsed.meta and parsed.meta.entity_keys else {}
    )
    # F = file
    f_rows: dict[str, list[dict]] = parsed.rows_by_sheet
    f_keys: dict[str, list[str]] = parsed.file_keys

    changes: list[Change] = []
    for sheet in SHEET_ORDER:
        f_map = {r["_row_key"]: r for r in f_rows.get(sheet, []) if r.get("_row_key")}
        g_map = {r["_row_key"]: r for r in g_rows.get(sheet, []) if r.get("_row_key")}
        b_set = set(b_keys.get(sheet, []))
        f_set = set(f_keys.get(sheet, []))
        g_set = set(g_keys.get(sheet, []))

        # 新增：F 有，G 无
        for k in sorted(f_set - g_set):
            changes.append(Change(
                type=sheet, key=k, op="add",
                row=f_map.get(k),
            ))
        # 更新：F∩G，字段不同
        for k in sorted(f_set & g_set):
            fd = _row_field_diff(sheet, f_map[k], g_map[k])
            if fd:
                changes.append(Change(
                    type=sheet, key=k, op="update", fields=fd,
                    row=f_map[k],
                ))
        # 删除：B 有，F 无，G 仍有 → 候选删除（引用计数后再定 conflict）
        delete_candidates = sorted((b_set - f_set) & g_set)
        for k in delete_candidates:
            ref = _check_references(driver, sheet, k, ds)
            if ref["blocked"]:
                changes.append(Change(
                    type=sheet, key=k, op="conflict",
                    reason=f"删除目标仍被引用：{ref['detail']}",
                ))
            else:
                changes.append(Change(
                    type=sheet, key=k, op="delete",
                    reason="in baseline, removed in file",
                ))

    summary = {
        "added": sum(1 for c in changes if c.op == "add"),
        "updated": sum(1 for c in changes if c.op == "update"),
        "deleted": sum(1 for c in changes if c.op == "delete"),
        "conflicts": sum(1 for c in changes if c.op == "conflict"),
    }
    return DiffResult(
        plan_id=f"imp_{uuid.uuid4().hex[:12]}",
        datasource_id=ds,
        graph_digest=g_bundle["meta"].graph_digest,
        changes=changes,
        summary=summary,
    )


def _check_references(driver: Driver, sheet: str, row_key: str, ds: str) -> dict[str, Any]:
    """删除引用计数守卫。返回 {blocked: bool, detail: str}。"""
    parts = row_key.split("$$$")
    detail = ""
    with neo4j_session(driver) as s:
        if sheet == "dataset":
            ds_name = parts[-1] if parts else ""
            cypher = """
            MATCH (ds:Dataset {name: $name})
            WHERE $ds = '' OR ds.datasource_id = $ds
            OPTIONAL MATCH (f:Formula)-[:OF_VIEW]->(ds)
            OPTIONAL MATCH (d:Dimension)-[:MAPS_TO_DATASET_COLUMN]->()-[:HAS_COLUMN]-(ds)
            RETURN count(DISTINCT f) AS f_cnt, count(DISTINCT d) AS d_cnt
            """
            rec = s.run(cypher, name=ds_name, ds=ds).single()
            f_cnt = int(rec["f_cnt"]) if rec else 0
            d_cnt = int(rec["d_cnt"]) if rec else 0
            if f_cnt or d_cnt:
                detail = f"formulas={f_cnt}, dimensions={d_cnt}"
                return {"blocked": True, "detail": detail}
        elif sheet == "dataset_column":
            if len(parts) >= 4:
                _domain, ds_name, col = parts[1], parts[2], parts[3]
                cypher = """
                MATCH (f:Formula)-[:USES_COLUMN]->(dc:DatasetColumn {name: $col})
                MATCH (f)-[:OF_VIEW]->(ds:Dataset {name: $ds_name})
                WHERE ($ds = '' OR ds.datasource_id = $ds)
                RETURN count(DISTINCT f) AS cnt
                """
                rec = s.run(cypher, col=col, ds_name=ds_name, ds=ds).single()
                cnt = int(rec["cnt"]) if rec else 0
                if cnt:
                    detail = f"used_by_formulas={cnt}"
                    return {"blocked": True, "detail": detail}
        elif sheet == "metric":
            metric_name = parts[-1] if parts else ""
            cypher = """
            MATCH (m:Metric {name: $name})
            WHERE $ds = '' OR m.datasource_id = $ds
            OPTIONAL MATCH (peer:Metric)-[:DERIVED_FROM]->(m)
            RETURN count(DISTINCT peer) AS cnt
            """
            rec = s.run(cypher, name=metric_name, ds=ds).single()
            cnt = int(rec["cnt"]) if rec else 0
            if cnt:
                detail = f"derived_by_metrics={cnt}"
                return {"blocked": True, "detail": detail}
    return {"blocked": False, "detail": ""}


# ---------------------------------------------------------------------- #
# 应用：add / update → upsert；delete → scoped DETACH DELETE
# ---------------------------------------------------------------------- #

def apply_diff(driver: Driver, diff: DiffResult) -> dict[str, int]:
    """应用 diff 计划（已通过漂移守卫）。返回实际计数。"""
    from .semantic_incremental_write import upsert_rows, delete_rows

    ds = diff.datasource_id
    applied = {"added": 0, "updated": 0, "deleted": 0, "skipped_conflict": 0}

    # add/update：按 sheet 分组 upsert
    add_update_by_sheet: dict[str, list[dict]] = {}
    for c in diff.changes:
        if c.op in ("add", "update") and c.row:
            add_update_by_sheet.setdefault(c.type, []).append(c.row)
    for sheet, rows in add_update_by_sheet.items():
        upsert_rows(driver, sheet, rows, datasource_id=ds)
    applied["added"] = sum(1 for c in diff.changes if c.op == "add")
    applied["updated"] = sum(1 for c in diff.changes if c.op == "update")

    # delete（conflict 跳过）
    delete_by_sheet: dict[str, list[Change]] = {}
    for c in diff.changes:
        if c.op == "delete":
            delete_by_sheet.setdefault(c.type, []).append(c)
        elif c.op == "conflict":
            applied["skipped_conflict"] += 1
    for sheet, cs in delete_by_sheet.items():
        applied["deleted"] += delete_rows(driver, sheet, [c.key for c in cs], datasource_id=ds)

    return applied


__all__ = ["Change", "DiffResult", "compute_diff", "apply_diff"]
