from __future__ import annotations

import json
from collections import defaultdict

import aiosqlite

from semantic_config.config import get_settings
from semantic_config.errors import BadRequestError

MODE_FULL = "FULL"


def _blank_to_none(s):
    if s is None:
        return None
    t = str(s).strip()
    return t or None


def _split_by(s, sep):
    if not s or not str(s).strip():
        return None
    parts = [p.strip() for p in str(s).split(sep)]
    parts = [p for p in parts if p]
    return parts or None


def _split_comma(s):
    return _split_by(s, ",")


def _split_triple_dollar(s):
    return _split_by(s, "$$$")


def _parse_tags(s):
    if not s or not str(s).strip():
        return None
    t = str(s).strip()
    if t.startswith("["):
        try:
            parsed = json.loads(t)
            if isinstance(parsed, list) and parsed:
                return [str(x) for x in parsed]
            return None
        except (ValueError, TypeError):
            pass
    return _split_comma(t)


def _prune(d: dict) -> dict:
    """对齐 Java 的 @JsonInclude(NON_NULL)：去掉 None / 空 list 的键（布尔 False 保留）。"""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        out[k] = v
    return out


async def _fetch_all(db: aiosqlite.Connection, sql: str, ds_id: str) -> list[aiosqlite.Row]:
    async with db.execute(sql, (ds_id,)) as cur:
        return await cur.fetchall()


async def assemble(db: aiosqlite.Connection, datasource_id: str, task_id: str) -> dict:
    """按 datasource 整库聚合语义信息，组装成 CM 导入载荷（结构对齐 docs/cm_demo.json）。

    datasource_id 为数据源对外编码，各子表 datasource_id 均存该编码。
    """
    async with db.execute(
        "SELECT * FROM datasource WHERE datasource_id = ? AND is_deleted = 0", (datasource_id,)
    ) as cur:
        ds = await cur.fetchone()
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={datasource_id}")

    domains = await _fetch_all(
        db, "SELECT * FROM biz_domain WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id", datasource_id
    )
    datasets = await _fetch_all(
        db, "SELECT * FROM dataset_meta WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id", datasource_id
    )
    columns = await _fetch_all(
        db, "SELECT * FROM dataset_column_meta WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id",
        datasource_id,
    )
    dimensions = await _fetch_all(
        db, "SELECT * FROM dimension WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id", datasource_id
    )
    bindings = await _fetch_all(
        db, "SELECT * FROM dataset_dimension WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id",
        datasource_id,
    )
    values = await _fetch_all(
        db, "SELECT * FROM dataset_dimension_value WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id",
        datasource_id,
    )
    metrics = await _fetch_all(
        db, "SELECT * FROM metric_lib WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id", datasource_id
    )
    formulas = await _fetch_all(
        db, "SELECT * FROM metric_formula_lib WHERE datasource_id = ? AND is_deleted = 0 ORDER BY id",
        datasource_id,
    )

    datasets_by_domain = defaultdict(list)
    for d in datasets:
        datasets_by_domain[d["domain_id"]].append(d)
    columns_by_dataset = defaultdict(list)
    for c in columns:
        columns_by_dataset[c["dataset_id"]].append(c)
    dims_by_domain = defaultdict(list)
    for d in dimensions:
        dims_by_domain[d["domain_id"]].append(d)
    bindings_by_dim = defaultdict(list)
    for b in bindings:
        bindings_by_dim[b["dimension_id"]].append(b)
    values_by_dim = defaultdict(list)
    for v in values:
        values_by_dim[v["dimension_id"]].append(v)
    metrics_by_domain = defaultdict(list)
    for m in metrics:
        metrics_by_domain[m["domain_id"]].append(m)
    formulas_by_metric = defaultdict(list)
    for f in formulas:
        formulas_by_metric[f["metric_id"]].append(f)

    dataset_id_to_name = {d["id"]: d["dataset_name"] for d in datasets}

    def build_columns(cols):
        out = []
        for c in cols:
            out.append(_prune({
                "name": c["column_name"],
                "name_cn": c["column_name_cn"],
                "comment": c["column_comment"],
                "enums": _split_triple_dollar(c["column_enums"]),
                "enums_description": _split_triple_dollar(c["column_enums_description"]),
                "samples": _split_triple_dollar(c["samples"]),
                "dimension_type": _blank_to_none(c["dimension_type"]),
            }))
        return out or None

    def build_datasets(dsets):
        out = []
        for d in dsets:
            out.append(_prune({
                "name": d["dataset_name"],
                "description": d["dataset_comment"],
                "dataset_type": d["dataset_type"],
                "sql": d["sql_content"],
                "parents": _split_comma(d["parents"]),
                "columns": build_columns(columns_by_dataset.get(d["id"], [])),
            }))
        return out or None

    def build_bindings(binds):
        out = []
        for b in binds:
            out.append(_prune({
                "dataset": dataset_id_to_name.get(b["dataset_id"]),
                "calculate_expr": b["calculate_expr"],
                "binding_type": b["dimension_type"],
                "data_type": b["data_type"],
            }))
        return out or None

    def build_values(vals):
        out = []
        seen = set()
        for v in vals:
            val = _blank_to_none(v["dimension_value"])
            if val and val not in seen:
                seen.add(val)
                out.append(_prune({"value": val}))
        return out or None

    def build_dimensions(dims):
        out = []
        for d in dims:
            out.append(_prune({
                "name": d["dimension_name"],
                "description": d["description"],
                # CM SemanticImportRequest.DimensionPayload 用 aliases（pydantic 校验会丢弃 synonyms）
                "aliases": _split_comma(d["synonyms"]),
                "parent_dimension": _blank_to_none(d["parent_name"]),
                "hierarchy_level": d["depth"],
                "is_display_dimension": None if d["is_visible"] is None else bool(d["is_visible"]),
                "is_contribution_dimension": None if d["is_attribution"] is None else bool(d["is_attribution"]),
                "bindings": build_bindings(bindings_by_dim.get(d["id"], [])),
                "values": build_values(values_by_dim.get(d["id"], [])),
            }))
        return out or None

    def build_formulas(fs):
        out = []
        for f in fs:
            out.append(_prune({
                "dataset": dataset_id_to_name.get(f["dataset_id"]),
                "formula": f["formula"],
                "formula_evidence": f["formula_evidence"],
                "date_range": f["date_range"],
                "derived_from": _blank_to_none(f["derived_from"]),
            }))
        return out or None

    def build_metrics(ms):
        out = []
        for m in ms:
            out.append(_prune({
                "name": m["metric_name"],
                "description": m["description"],
                "unit": m["unit"],
                "is_north_star": None if m["is_polaris"] is None else bool(m["is_polaris"]),
                "is_display_distribution": None if m["show_distribution"] is None else bool(m["show_distribution"]),
                "is_display": None if m["is_visible"] is None else bool(m["is_visible"]),
                # CM SemanticImportRequest.MetricPayload 用 aliases（pydantic 校验会丢弃 synonyms）
                "aliases": _split_comma(m["synonyms"]),
                "tags": _parse_tags(m["tags"]),
                "formulas": build_formulas(formulas_by_metric.get(m["id"], [])),
            }))
        return out or None

    domain_nodes = []
    for dom in domains:
        domain_nodes.append(_prune({
            "name": dom["domain_name"],
            "display_name": dom["display_name"],
            "description": dom["description"],
            "aliases": _split_comma(dom["aliases"]),
            "datasets": build_datasets(datasets_by_domain.get(dom["id"], [])),
            "dimensions": build_dimensions(dims_by_domain.get(dom["id"], [])),
            "metrics": build_metrics(metrics_by_domain.get(dom["id"], [])),
        }))

    settings = get_settings()
    # 顶层结构对齐 CM 的 SemanticImportRequest（context_manager/api/import_models.py）。
    # datasource_id 与数据源注册时一致；CM 侧用它做 drop_semantic / db_id 兜底 / 语义节点 scope 打标。
    # db_id 留空，交由 CM 侧从 datasource_id 自动推导。
    return {
        "datasource_id": ds["datasource_id"],
        "db_id": "",
        "schema_name": settings.weave_schema_name or "public",
        "semantic": _prune({"domains": domain_nodes or None}),
        "drop_semantic_first": True,
        "task_id": task_id,
        "callback_url": settings.weave_callback_url,
    }
