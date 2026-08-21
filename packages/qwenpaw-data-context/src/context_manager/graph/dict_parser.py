"""嵌套 SemanticPayload → ``metrics_dict`` 格式转换。

前端配置平台推送的嵌套结构经解析后转换为
``metrics_dict.yaml`` 兼容结构，再交由
:func:`context_manager.graph.semantic.ingest_semantic` 写入 Neo4j。
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from ..utils import get_logger

log = get_logger("graph.dict_parser")


def parse_nested_semantic_payload(
    payload: dict[str, Any],
    *,
    db_id: str,
    schema: str = "public",
) -> dict[str, Any]:
    """把前端推送的嵌套 SemanticPayload 转换为 metrics_dict 格式。

    输入：``SemanticPayload.model_dump()`` 形态——``{"domains": [{name, datasets, dimensions, metrics}]}``
    输出：``ingest_semantic`` 能直接吃的 metrics_dict 形态——见 ``metrics_dict.yaml`` schema

    关键映射：
    - 每个 domain 的 ``datasets[]`` 建一个 *logical name → physical qualified table* 映射；
      ``dimension.bindings[].dataset`` / ``formula.dataset`` 通过此映射解析到 ``db.schema.table``
    - ``binding.calculate_expr`` 用正则 ``select(X)`` 提取目标列；提不出来时退化为维度名作为列名
    - ``dataset.sql`` / ``binding.binding_type`` / 多 binding 不同 expr 等暂时落到 dim 节点属性，
      等待拓扑改造后会自动落到对应边上（详见导入接口文档）
    """
    if not isinstance(payload, dict):
        raise ValueError(f"semantic payload must be a dict, got {type(payload).__name__}")

    domains_out: list[dict[str, Any]] = []
    domains_in = list(payload.get("domains") or [])

    for dom in domains_in:
        if not isinstance(dom, dict):
            continue
        dom_name = str(dom.get("name") or "").strip()
        if not dom_name:
            continue

        datasets_in = list(dom.get("datasets") or [])
        dataset_lookup = _build_dataset_lookup(datasets_in, db_id=db_id, schema=schema)

        domain_entry: dict[str, Any] = {
            "name": dom_name,
            "display_name": str(dom.get("display_name") or dom_name),
            "description": str(dom.get("description") or ""),
            "aliases": _str_list(dom.get("aliases")),
            "status": "stable",
        }

        datasets_out = _convert_datasets(datasets_in, dom_name, dataset_lookup)
        if datasets_out:
            domain_entry["datasets"] = datasets_out

        ds_cols_out = _convert_dataset_columns(datasets_in, dom_name, dataset_lookup)
        if ds_cols_out:
            domain_entry["dataset_columns"] = ds_cols_out

        dims_out = _convert_dimensions(
            list(dom.get("dimensions") or []),
            dataset_lookup=dataset_lookup,
        )
        if dims_out:
            domain_entry["dimensions"] = dims_out

        metrics_out = _convert_metrics(
            list(dom.get("metrics") or []),
            dataset_lookup=dataset_lookup,
        )
        if metrics_out:
            domain_entry["metrics"] = metrics_out

        domains_out.append(domain_entry)

    result: dict[str, Any] = {
        "version": "nested-payload",
        "domains": domains_out,
        "operators": [],
    }
    log.info(
        "nested semantic payload parsed: %d domains, %d dimensions, %d metrics",
        len(domains_out),
        sum(len(d.get("dimensions", [])) for d in domains_out),
        sum(len(d.get("metrics", [])) for d in domains_out),
    )
    return result


def save_as_temp_yaml(data: dict[str, Any]) -> Path:
    """将 dict 序列化为 YAML 并写入临时文件，返回路径。

    用于将解析结果喂给 ``ingest_semantic``（它只接受 Path）。
    """
    import yaml

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="import_dict_", delete=False, encoding="utf-8"
    )
    yaml.dump(data, tmp, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.close()
    log.debug("temp yaml written: %s", tmp.name)
    return Path(tmp.name)


# ---------------------------------------------------------------------- #
# 嵌套 SemanticPayload 解析的内部工具
# ---------------------------------------------------------------------- #
def _str_list(val: Any) -> list[str]:
    """规范化为 ``list[str]``，None/空值返回 ``[]``。自动拆分 ``$$$`` 分隔符。"""
    if val is None:
        return []
    if isinstance(val, list):
        out: list[str] = []
        for v in val:
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            for p in (s.split("$$$") if "$$$" in s else [s]):
                p = p.strip()
                if p:
                    out.append(p)
        return out
    s = str(val).strip()
    if not s:
        return []
    if "$$$" in s:
        return [p.strip() for p in s.split("$$$") if p.strip()]
    return [s]


# 提取 select(col) / select(col1,col2) 中的目标列；优先取最后一个，覆盖 EAV 模式
_SELECT_COL_RE = re.compile(r"select\s*\(\s*([\w一-龥]+)", re.IGNORECASE)
# 提取 where(col=...) 里的过滤列名（用于 EAV 模式的 dim_type 过滤）
_WHERE_FILTER_RE = re.compile(
    r"where\s*\(\s*([\w一-龥]+)\s*=\s*['\"]?([^'\")]+)['\"]?\s*\)",
    re.IGNORECASE,
)


def _build_dataset_lookup(
    datasets: list[dict[str, Any]],
    *,
    db_id: str,
    schema: str,
) -> dict[str, dict[str, str]]:
    """构建 *logical dataset name → {table, qualified_table}* 映射。"""
    out: dict[str, dict[str, str]] = {}
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        name = str(ds.get("name") or "").strip()
        if not name:
            continue
        parents = ds.get("parents") or []
        if not isinstance(parents, list) or not parents:
            log.warning("dataset %r has no parents, formula/binding 解析会失败", name)
            continue
        physical_table = str(parents[0]).strip()
        if not physical_table:
            continue
        # parents[0] 可能已经是 qualified (db.schema.table) 也可能只是表名
        if physical_table.count(".") >= 2:
            qualified = physical_table
        elif "." in physical_table:
            qualified = f"{db_id}.{physical_table}"
        else:
            qualified = f"{db_id}.{schema}.{physical_table}"
        out[name] = {
            "table": physical_table,
            "qualified": qualified,
            "schema": schema,
            "db_id": db_id,
        }
    return out


def _convert_datasets(
    datasets: list, domain: str, lookup: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """Convert raw dataset dicts into the format expected by ``write_datasets``."""
    out: list[dict[str, Any]] = []
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        name = str(ds.get("name") or "").strip()
        if not name:
            continue
        info = lookup.get(name) or {}
        parents = ds.get("parents") or []
        parent_str = str(parents[0]).strip() if parents else ""
        out.append({
            "name": name,
            "domain": domain,
            "physical_table": info.get("table") or parent_str or name,
            "description": str(ds.get("description") or ""),
            "dataset_type": str(ds.get("dataset_type") or "OLAP") or "OLAP",
            "sql": str(ds.get("sql") or ""),
            "parents": parent_str,
            "filter_summary": str(ds.get("filter_summary") or ""),
        })
    return out


def _convert_dataset_columns(
    datasets: list, domain: str, lookup: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """Convert raw dataset column dicts into the format expected by ``write_dataset_columns``."""
    out: list[dict[str, Any]] = []
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        ds_name = str(ds.get("name") or "").strip()
        if not ds_name:
            continue
        info = lookup.get(ds_name) or {}
        parents = ds.get("parents") or []
        parent_str = str(parents[0]).strip() if parents else ""
        physical_table = info.get("table") or parent_str or ds_name
        for col in ds.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("name") or "").strip()
            if not col_name:
                continue
            out.append({
                "domain": domain,
                "dataset_name": ds_name,
                "col_name": col_name,
                "physical_table": physical_table,
                "data_type": str(col.get("data_type") or ""),
                "comment": str(col.get("comment") or ""),
                "is_primary": bool(col.get("is_primary") or False),
                "is_nullable": bool(col.get("is_nullable") or True),
            })
    return out


def _extract_binding_column(calculate_expr: str, fallback: str) -> tuple[str, str]:
    """从 calculate_expr 提取 (目标列, 过滤子句)。

    - ``select(terminal_type)`` → ("terminal_type", "")
    - ``where(dimension_type='X').select(dimension_value)`` → ("dimension_value", "dimension_type='X'")
    - 解析不出来 → (fallback, "")
    """
    expr = (calculate_expr or "").strip()
    if not expr:
        return fallback, ""
    sel = _SELECT_COL_RE.search(expr)
    target_col = sel.group(1).strip() if sel else fallback
    wh = _WHERE_FILTER_RE.search(expr)
    filter_clause = f"{wh.group(1).strip()}='{wh.group(2).strip()}'" if wh else ""
    return target_col, filter_clause


def _convert_dimensions(
    dims_in: list[dict[str, Any]],
    *,
    dataset_lookup: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dim in dims_in:
        if not isinstance(dim, dict):
            continue
        name = str(dim.get("name") or "").strip()
        if not name:
            continue

        bindings = list(dim.get("bindings") or [])
        maps_to_columns: list[dict[str, str]] = []
        first_binding: dict[str, Any] = {}

        for b in bindings:
            if not isinstance(b, dict):
                continue
            ds_logical = str(b.get("dataset") or "").strip()
            ds_meta = dataset_lookup.get(ds_logical)
            if not ds_meta:
                log.warning(
                    "dimension %r binding references unknown dataset %r; skipping",
                    name, ds_logical,
                )
                continue
            calc_expr = str(b.get("calculate_expr") or "").strip()
            col_name, filter_clause = _extract_binding_column(calc_expr, fallback=name)
            qualified_col = f"{ds_meta['qualified']}.{col_name}"
            maps_to_columns.append(
                {
                    "column": qualified_col,
                    "expr": calc_expr,
                    "filter": filter_clause,
                }
            )
            if not first_binding:
                first_binding = b

        dim_entry: dict[str, Any] = {
            "name": name,
            "type": "enum",
            "description": str(dim.get("description") or ""),
            "synonyms": _str_list(dim.get("synonyms")),
            "hierarchy_level": int(dim.get("hierarchy_level") or 0),
            "is_display_dimension": bool(dim.get("is_display_dimension", True)),
            "is_contribution_dimension": bool(dim.get("is_contribution_dimension", True)),
            "status": "stable",
        }
        parent = str(dim.get("parent_dimension") or "").strip()
        if parent:
            dim_entry["parent"] = parent
        if first_binding:
            # 取首条 binding 的 dataset/binding_type/data_type 落到节点（暂时性，等拓扑改造 ① 上提到边）
            ds_meta = dataset_lookup.get(str(first_binding.get("dataset") or ""))
            if ds_meta:
                dim_entry["dataset_name"] = ds_meta["qualified"]
            bt = str(first_binding.get("binding_type") or "").strip()
            if bt:
                dim_entry["dimension_type"] = bt
            dt = str(first_binding.get("data_type") or "").strip()
            if dt:
                dim_entry["data_type"] = dt
            # calculate_expr now lives on MAPS_TO edge as expr, not on the node
        if maps_to_columns:
            dim_entry["maps_to_columns"] = maps_to_columns

        # values: [{value, aliases}] → [{value, label}]
        values_in = list(dim.get("values") or [])
        if values_in:
            values_out: list[dict[str, Any]] = []
            for v in values_in:
                if not isinstance(v, dict):
                    continue
                val = str(v.get("value") or "").strip()
                if not val:
                    continue
                aliases = _str_list(v.get("aliases"))
                values_out.append({"value": val, "label": val, "aliases": aliases})
            if values_out:
                dim_entry["values"] = values_out

        out.append(dim_entry)
    return out


def _convert_metrics(
    metrics_in: list[dict[str, Any]],
    *,
    dataset_lookup: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for met in metrics_in:
        if not isinstance(met, dict):
            continue
        name = str(met.get("name") or "").strip()
        if not name:
            continue

        formulas_out: list[dict[str, Any]] = []
        for f in list(met.get("formulas") or []):
            if not isinstance(f, dict):
                continue
            ds_logical = str(f.get("dataset") or "").strip()
            ds_meta = dataset_lookup.get(ds_logical)
            if not ds_meta:
                log.warning(
                    "metric %r formula references unknown dataset %r; skipping",
                    name, ds_logical,
                )
                continue
            evidence_col = str(f.get("formula_evidence") or "").strip()
            formula_expr = str(f.get("formula") or "").strip()
            if not formula_expr:
                if evidence_col and "." not in evidence_col:
                    # 前端只推 formula_evidence（列名）没推 formula → 用列名兜底
                    formula_expr = evidence_col
                    log.info(
                        "metric %r: formula empty, falling back to formula_evidence=%r",
                        name, evidence_col,
                    )
                else:
                    continue
            f_entry: dict[str, Any] = {
                "dataset": ds_meta["qualified"],
                "formula": formula_expr,
                "formula_evidence": evidence_col,
                "date_range": str(f.get("date_range") or ""),
                "is_primary": bool(f.get("is_primary", False)),
            }
            if evidence_col and "." not in evidence_col:
                f_entry["uses_columns"] = [
                    {
                        "column": f"{ds_meta['qualified']}.{evidence_col}",
                        "role": "numerator",
                    }
                ]
            formulas_out.append(f_entry)

        met_entry: dict[str, Any] = {
            "name": name,
            "type": "quantity",
            "description": str(met.get("description") or ""),
            "unit": str(met.get("unit") or ""),
            "is_north_star": bool(met.get("is_north_star", False)),
            "is_display": bool(met.get("is_display", True)),
            "is_display_distribution": bool(met.get("is_display_distribution", True)),
            "synonyms": _str_list(met.get("synonyms")),
            "tags": _str_list(met.get("tags")),
            "status": "stable",
        }
        analyzed_by = _str_list(met.get("analyzed_by")) or _str_list(met.get("can_drill_by"))
        if analyzed_by:
            met_entry["analyzed_by"] = analyzed_by
        derived_from = list(met.get("derived_from") or [])
        if derived_from:
            met_entry["derived_from"] = [
                {
                    "metric_name": str(r.get("metric_name") or ""),
                    "relation_type": str(r.get("relation_type") or "ratio_decompose"),
                    "role": str(r.get("role") or ""),
                }
                for r in derived_from
                if isinstance(r, dict) and r.get("metric_name")
            ]
        if formulas_out:
            met_entry["formulas"] = formulas_out
        out.append(met_entry)
    return out
