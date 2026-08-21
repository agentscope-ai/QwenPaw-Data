"""Neo4j-backed read store for Semantic Layer REST."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from neo4j import Driver

from ..utils import neo4j_session
from ..graph.keys import dim_key, metric_key
from ..graph.semantic import TOPLINE_LITERALS_RICH
from ..graph.semantic_fields import (
    anomaly_rules_from_json,
    metric_role_from_props,
)
from .semantic_models import (
    ColumnMeta,
    Dataset,
    DatasetSchema,
    Dimension,
    DimensionHierarchy,
    Domain,
    MetricDetail,
    MetricDimensionBinding,
    MetricFormula,
    MetricNorthStarSummary,
    MetricSummary,
)


@dataclass(frozen=True)
class DimensionValueRecord:
    value: str
    is_rollup: bool

log = logging.getLogger("api.semantic_store")

_SEARCH_MISS_MESSAGE = (
    "未检索到匹配指标，建议先查看当前 domain 的全量指标列表再确认。"
)


def _domain_ds_clause(alias: str = "dom") -> str:
    """Datasource filter appended into a WHERE clause on a Domain-bound node.

    Empty ``$ds`` disables the filter (legacy / single-source fallback):
    every semantic node carries a ``datasource_id`` property post-backfill,
    but ``datasource_id`` being unset on pre-isolation graphs means the
    ``$ds = ''`` short-circuit keeps reads working without a source argument.
    """
    return f"($ds = '' OR {alias}.datasource_id = $ds)"


def _str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for x in val:
            if x is None:
                continue
            s = str(x)
            # 兜底：Neo4j 里可能存了未拆的 ``$$$`` 拼接字符串
            for p in (s.split("$$$") if "$$$" in s else [s]):
                p = p.strip()
                if p:
                    out.append(p)
        return out
    s = str(val)
    return [p.strip() for p in (s.split("$$$") if "$$$" in s else [s]) if p.strip()]


def _metric_summary_from_row(row: dict[str, Any]) -> MetricSummary:
    return MetricSummary(
        metric_name=str(row.get("name") or ""),
        aliases=_str_list(row.get("aliases")),
        tags=_str_list(row.get("tags")),
        role=metric_role_from_props(row),
    )


def _metric_north_star_from_row(row: dict[str, Any]) -> MetricNorthStarSummary:
    return MetricNorthStarSummary(
        metric_name=str(row.get("name") or ""),
        description=str(row.get("description") or ""),
        aliases=_str_list(row.get("aliases")),
        tags=_str_list(row.get("tags")),
    )


def _column_type_from_props(col: dict[str, Any]) -> str:
    ctype = str(col.get("type") or "").lower()
    if ctype in ("date", "datetime", "timestamp"):
        return "日期"
    role = str(col.get("granularity_role") or "").lower()
    if role in ("partition", "split_no_topline") or "dim" in ctype:
        return "维度"
    return "度量"


# ---------------------------------------------------------------------- #
# Domains
# ---------------------------------------------------------------------- #

def list_domain_records(driver: Driver, datasource_id: str = "") -> list[Domain]:
    ds = (datasource_id or "").strip()
    cypher = """
    MATCH (d:Domain)
    WHERE ($ds = '' OR d.datasource_id = $ds)
    RETURN d.name AS name,
           coalesce(d.display_name, d.name) AS display_name,
           coalesce(d.description, '') AS description,
           coalesce(d.aliases, []) AS aliases,
           coalesce(d.datasource_id, '') AS datasource_id
    ORDER BY datasource_id, name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, ds=ds).data()
    out: list[Domain] = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        out.append(
            Domain(
                name=name,
                display_name=str(r.get("display_name") or name),
                description=str(r.get("description") or ""),
                aliases=_str_list(r.get("aliases")),
                datasource_id=str(r.get("datasource_id") or ""),
            )
        )
    return out


def domain_exists(driver: Driver, domain: str, datasource_id: str = "") -> bool:
    with neo4j_session(driver) as s:
        rec = s.run(
            "MATCH (d:Domain {name: $domain}) "
            "WHERE ($ds = '' OR d.datasource_id = $ds) "
            "RETURN d LIMIT 1",
            domain=domain,
            ds=(datasource_id or "").strip(),
        ).single()
    return rec is not None


def require_domain(driver: Driver, domain: str, datasource_id: str = "") -> None:
    if not domain_exists(driver, domain, datasource_id):
        raise KeyError(
            f"Domain not found: {domain}"
            + (f" @{datasource_id}" if datasource_id else "")
        )


# ---------------------------------------------------------------------- #
# Metrics
# ---------------------------------------------------------------------- #

def _active_metric_clause(alias: str = "m") -> str:
    return f"({alias}.valid_to IS NULL OR {alias}.valid_to > datetime())"


def _load_metric_formulas(driver: Driver, m_key: str) -> list[MetricFormula]:
    cypher = """
    MATCH (m:Metric {key: $m_key})-[:HAS_FORMULA]->(f:Formula)
    RETURN f.dataset AS dataset, f.formula AS formula,
           coalesce(f.formula_evidence, '') AS formula_evidence,
           coalesce(f.date_range, f.refresh_freq, '') AS date_range
    ORDER BY dataset
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, m_key=m_key).data()
    return [
        MetricFormula(
            dataset=str(r.get("dataset") or ""),
            formula=str(r.get("formula") or ""),
            formula_evidence=str(r.get("formula_evidence") or ""),
            date_range=str(r.get("date_range") or ""),
        )
        for r in rows
    ]


def list_metrics(
    driver: Driver, domain: str, datasource_id: str = "",
) -> list[MetricSummary]:
    require_domain(driver, domain, datasource_id)
    cypher = f"""
    MATCH (dom:Domain {{name: $domain}})-[:HAS_METRIC]->(m:Metric)
    WHERE {_active_metric_clause("m")} AND {_domain_ds_clause("dom")}
    RETURN m.name AS name, m.aliases AS aliases, m.tags AS tags,
           m.is_north_star AS is_north_star, m.is_display AS is_display,
           m.is_display_distribution AS is_display_distribution, m.role AS role
    ORDER BY m.name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, domain=domain, ds=(datasource_id or "").strip()).data()
    return [_metric_summary_from_row(r) for r in rows if r.get("name")]


def list_north_star_metrics(
    driver: Driver, domain: str, datasource_id: str = "",
) -> list[MetricNorthStarSummary]:
    require_domain(driver, domain, datasource_id)
    cypher = f"""
    MATCH (dom:Domain {{name: $domain}})-[:HAS_METRIC]->(m:Metric)
    WHERE {_active_metric_clause("m")} AND {_domain_ds_clause("dom")}
      AND (m.is_north_star = true OR m.role = 'north_star')
    RETURN m.name AS name,
           coalesce(m.description, '') AS description,
           m.aliases AS aliases,
           m.tags AS tags
    ORDER BY m.name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, domain=domain, ds=(datasource_id or "").strip()).data()
    return [_metric_north_star_from_row(r) for r in rows if r.get("name")]


def search_metrics(
    driver: Driver,
    domain: str,
    query: str,
    *,
    k: int = 10,
    datasource_id: str = "",
) -> list[MetricSummary] | dict[str, Any]:
    require_domain(driver, domain, datasource_id)
    from .retrieval import resolve_metric

    rows = resolve_metric(driver, query, k=k, domain=domain)
    if not rows:
        return {"items": [], "message": _SEARCH_MISS_MESSAGE}
    summaries: list[MetricSummary] = []
    for r in rows:
        name = str(r.get("name") or "")
        if not name:
            continue
        summaries.append(
            MetricSummary(
                metric_name=name,
                aliases=_str_list(r.get("aliases")),
                tags=_str_list(r.get("tags")),
                role=metric_role_from_props(r),
            )
        )
    return summaries


def _resolve_metric_key(
    driver: Driver, domain: str, metric_name: str, datasource_id: str = "",
) -> str:
    """Resolve path/query name to graph ``Metric.key`` (name or synonym)."""
    name = (metric_name or "").strip()
    if not name:
        raise KeyError(f"Metric not found: {domain}::")
    ds = (datasource_id or "").strip()
    m_key = metric_key(domain, name, datasource_id)
    with neo4j_session(driver) as s:
        if s.run(
            "MATCH (m:Metric {key: $key}) RETURN m LIMIT 1",
            key=m_key,
        ).single():
            return m_key
        rec = s.run(
            """
            MATCH (m:Metric)
            WHERE m.domain = $domain
              AND (m.name = $name OR $name IN coalesce(m.aliases, []))
              AND ($ds = '' OR m.datasource_id = $ds)
            RETURN m.key AS key
            ORDER BY CASE WHEN m.name = $name THEN 0 ELSE 1 END
            LIMIT 1
            """,
            domain=domain,
            name=name,
            ds=ds,
        ).single()
        if rec and rec.get("key"):
            return str(rec["key"])
    raise KeyError(f"Metric not found: {domain}::{metric_name}")


def get_metric_detail(
    driver: Driver, domain: str, metric_name: str, datasource_id: str = "",
) -> MetricDetail:
    require_domain(driver, domain, datasource_id)
    m_key = _resolve_metric_key(driver, domain, metric_name, datasource_id)
    from .retrieval import expand_subgraph

    sg = expand_subgraph(driver, m_key)
    center = sg.get("center")
    if not center:
        raise KeyError(f"Metric not found: {domain}::{metric_name}")

    raw = sg.get("raw") or {}
    metric_props = raw.get("metric") or {}
    formulas = _load_metric_formulas(driver, m_key)
    if not metric_props:
        # fallback: load metric node via scoped-Domain traversal + name match
        # (avoids rebuilding the key; honors datasource isolation)
        with neo4j_session(driver) as s:
            rec = s.run(
                f"""
                MATCH (dom:Domain {{name: $domain}})-[:HAS_METRIC]->(m:Metric {{name: $metric_name}})
                WHERE {_active_metric_clause("m")} AND {_domain_ds_clause("dom")}
                RETURN m
                LIMIT 1
                """,
                domain=domain,
                metric_name=(metric_name or "").strip(),
                ds=(datasource_id or "").strip(),
            ).single()
        if not rec:
            raise KeyError(f"Metric not found: {domain}::{metric_name}")
        metric_props = dict(rec["m"])

    bindings = get_dimensions_for_metric(
        driver, domain, metric_name, metric_key_actual=m_key, datasource_id=datasource_id,
    )

    return MetricDetail(
        metric_name=str(metric_props.get("name") or metric_name),
        domain=domain,
        aliases=_str_list(metric_props.get("aliases")),
        tags=_str_list(metric_props.get("tags")),
        role=metric_role_from_props(metric_props),
        description=str(metric_props.get("description") or ""),
        unit=str(metric_props.get("unit") or ""),
        formulas=formulas,
        anomaly_rules=anomaly_rules_from_json(
            metric_props.get("anomaly_rules_json") or raw.get("anomaly_rules")
        ),
        dimensions=bindings,
    )


def get_dimensions_for_metric(
    driver: Driver,
    domain: str,
    metric_name: str,
    *,
    metric_key_actual: Optional[str] = None,
    datasource_id: str = "",
) -> list[MetricDimensionBinding]:
    m_key = metric_key_actual or metric_key(domain, metric_name, datasource_id)
    ds = (datasource_id or "").strip()
    # Prefer explicit ANALYZED_BY edges when present; otherwise derive from
    # Dataset topology: Metric→Formula→Dataset→DatasetColumn←Dimension (import API)
    # or physical Table: Metric→Formula→Dataset→Table→Column←Dimension (legacy).
    cypher_explicit = f"""
    MATCH (m:Metric {{key: $m_key}})-[r:ANALYZED_BY]->(d:Dimension)
    WHERE d.domain = $domain AND {_domain_ds_clause("d")}
    RETURN d.name AS name,
           coalesce(r.is_display_dimension, true) AS is_display_dimension,
           coalesce(r.is_contribution_dimension, true) AS is_contribution_dimension
    ORDER BY d.name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher_explicit, m_key=m_key, domain=domain, ds=ds).data()
    if not rows:
        # Indirect fallback: Metric→Formula→Dataset→DatasetColumn←Dimension
        # (import API creates MAPS_TO_DATASET_COLUMN; excel uses MAPS_TO_COLUMN)
        cypher_indirect = f"""
        MATCH (m:Metric {{key: $m_key}})-[:HAS_FORMULA]->(:Formula)-[:OF_VIEW]->(ds:Dataset)
        WHERE ds.domain = $domain
        MATCH (ds)-[:HAS_COLUMN]->(dc:DatasetColumn)<-[:MAPS_TO_DATASET_COLUMN]-(d:Dimension)
        WHERE d.domain = $domain AND {_domain_ds_clause("d")}
        RETURN DISTINCT d.name AS name
        ORDER BY name
        """
        with neo4j_session(driver) as s:
            rows = s.run(cypher_indirect, m_key=m_key, domain=domain, ds=ds).data()
    if not rows:
        # Second indirect fallback: via physical Table (legacy path)
        cypher_indirect_legacy = f"""
        MATCH (m:Metric {{key: $m_key}})-[:HAS_FORMULA]->(:Formula)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t:Table)
        MATCH (t)-[:HAS_COLUMN]->(c:Column)<-[:MAPS_TO_COLUMN]-(d:Dimension)
        WHERE d.domain = $domain AND {_domain_ds_clause("d")}
        RETURN DISTINCT d.name AS name
        ORDER BY name
        """
        with neo4j_session(driver) as s:
            rows = s.run(cypher_indirect_legacy, m_key=m_key, domain=domain, ds=ds).data()
    if not rows:
        # metric exists but no bindings — not an error for this endpoint
        with neo4j_session(driver) as s:
            exists = s.run(
                "MATCH (m:Metric {key: $m_key}) "
                "WHERE $ds = '' OR m.datasource_id = $ds "
                "RETURN m LIMIT 1",
                m_key=m_key,
                ds=ds,
            ).single()
        if not exists:
            raise KeyError(f"Metric not found: {domain}::{metric_name}")
    return [
        MetricDimensionBinding(
            dimension_name=str(r.get("name") or ""),
            is_display_dimension=bool(r.get("is_display_dimension", True)),
            is_contribution_dimension=bool(r.get("is_contribution_dimension", True)),
        )
        for r in rows
        if r.get("name")
    ]


def get_metrics_for_dimension(
    driver: Driver,
    domain: str,
    dim_name: str,
    datasource_id: str = "",
) -> list[MetricSummary]:
    """反向查询：给定维度，返回所有可通过该维度分析的指标。

    Falls back to indirect path (shared physical Table) when no
    ANALYZED_BY edges exist — mirrors `get_dimensions_for_metric`.
    """
    require_domain(driver, domain, datasource_id)
    d_key = dim_key(domain, dim_name, datasource_id)
    ds = (datasource_id or "").strip()
    cypher_explicit = f"""
    MATCH (m:Metric)-[r:ANALYZED_BY]->(d:Dimension {{key: $d_key}})
    WHERE m.domain = $domain AND {_active_metric_clause("m")}
      AND {_domain_ds_clause("d")} AND {_domain_ds_clause("m")}
    RETURN m.name AS name,
           coalesce(m.description, '') AS description,
           coalesce(m.aliases, []) AS aliases,
           coalesce(m.tags, []) AS tags,
           m.is_north_star AS is_north_star,
           m.is_display AS is_display,
           m.is_display_distribution AS is_display_distribution,
           m.role AS role
    ORDER BY m.name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher_explicit, d_key=d_key, domain=domain, ds=ds).data()
    if not rows:
        cypher_indirect = f"""
        MATCH (d:Dimension {{key: $d_key}})-[:MAPS_TO_COLUMN]->(:Column)<-[:HAS_COLUMN]-(t:Table)
        MATCH (m:Metric)-[:HAS_FORMULA]->(:Formula)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t)
        WHERE m.domain = $domain AND {_active_metric_clause("m")}
          AND {_domain_ds_clause("d")} AND {_domain_ds_clause("m")}
        RETURN DISTINCT m.name AS name,
               coalesce(m.description, '') AS description,
               coalesce(m.aliases, []) AS aliases,
               coalesce(m.tags, []) AS tags,
               m.is_north_star AS is_north_star,
               m.is_display AS is_display,
               m.is_display_distribution AS is_display_distribution,
               m.role AS role
        ORDER BY name
        """
        with neo4j_session(driver) as s:
            rows = s.run(cypher_indirect, d_key=d_key, domain=domain, ds=ds).data()
    if not rows:
        # dimension exists but no metrics bound — not an error
        with neo4j_session(driver) as s:
            exists = s.run(
                "MATCH (d:Dimension {key: $d_key}) "
                "WHERE $ds = '' OR d.datasource_id = $ds "
                "RETURN d LIMIT 1",
                d_key=d_key,
                ds=ds,
            ).single()
        if not exists:
            raise KeyError(f"Dimension not found: {domain}::{dim_name}")
    from ..graph.semantic_fields import metric_role_from_props
    return [
        MetricSummary(
            metric_name=str(r.get("name") or ""),
            description=str(r.get("description") or ""),
            aliases=_str_list(r.get("aliases")),
            tags=_str_list(r.get("tags")),
            role=metric_role_from_props(r),
        )
        for r in rows
        if r.get("name")
    ]


# ---------------------------------------------------------------------- #
# Dimensions
# ---------------------------------------------------------------------- #

def list_dimension_names(
    driver: Driver, domain: str, datasource_id: str = "",
) -> list[str]:
    require_domain(driver, domain, datasource_id)
    cypher = f"""
    MATCH (d:Dimension {{domain: $domain}})
    WHERE {_domain_ds_clause("d")}
    RETURN DISTINCT d.name AS name
    ORDER BY name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, domain=domain, ds=(datasource_id or "").strip()).data()
    return [str(r["name"]) for r in rows if r.get("name")]


def get_dimension(
    driver: Driver, domain: str, dim_name: str, datasource_id: str = "",
) -> Dimension:
    require_domain(driver, domain, datasource_id)
    d_key = dim_key(domain, dim_name, datasource_id)
    cypher = f"""
    MATCH (d:Dimension {{key: $d_key}})
    WHERE d.domain = $domain AND {_domain_ds_clause("d")}
    OPTIONAL MATCH (d)-[mc:MAPS_TO_DATASET_COLUMN|MAPS_TO_COLUMN]->(target)
      WHERE target:DatasetColumn OR target:Column
    OPTIONAL MATCH (target)<-[:HAS_COLUMN]-(ds:Dataset)
    OPTIONAL MATCH (d)-[:HAS_PARENT]->(parent:Dimension)
    RETURN d,
           coalesce(ds.name, '') AS dataset_name,
           coalesce(mc.expr, '') AS calculate_expr,
           coalesce(parent.name, '') AS parent_name
    LIMIT 1
    """
    with neo4j_session(driver) as s:
        rec = s.run(
            cypher, d_key=d_key, domain=domain, ds=(datasource_id or "").strip(),
        ).single()
    if not rec or rec["d"] is None:
        raise KeyError(f"Dimension not found: {domain}::{dim_name}")

    props = dict(rec["d"])
    expr = str(rec.get("calculate_expr") or "")
    if expr and not expr.lower().startswith("select"):
        expr = f"select({expr})"

    dim_type = str(props.get("dimension_type") or "OLAP维度")

    return Dimension(
        dimension_name=str(props.get("name") or dim_name),
        domain=domain,
        dataset_name=str(rec.get("dataset_name") or props.get("dataset_name") or ""),
        calculate_expr=expr,
        dimension_type=dim_type,
        data_type=str(props.get("data_type") or "text"),
        aliases=_str_list(props.get("aliases")),
        parent_dimension=str(rec.get("parent_name") or props.get("parent_dimension") or ""),
        hierarchy_level=int(props.get("hierarchy_level") or 0),
        is_display_dimension=bool(props.get("is_display_dimension", True)),
        is_contribution_dimension=bool(props.get("is_contribution_dimension", True)),
    )


def get_dimension_hierarchy(
    driver: Driver, domain: str, dim_name: str, datasource_id: str = "",
) -> DimensionHierarchy:
    get_dimension(driver, domain, dim_name, datasource_id)  # ensure exists
    d_key = dim_key(domain, dim_name, datasource_id)
    cypher = f"""
    MATCH (d:Dimension {{key: $d_key}})
    WHERE {_domain_ds_clause("d")}
    OPTIONAL MATCH (d)-[:HAS_PARENT]->(p:Dimension)
    WITH d, collect(DISTINCT p.name) AS parents
    OPTIONAL MATCH (child:Dimension)-[:HAS_PARENT]->(d)
    RETURN parents, collect(DISTINCT child.name) AS children
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, d_key=d_key, ds=(datasource_id or "").strip()).single()
    parents = [str(x) for x in (rec.get("parents") or []) if x] if rec else []
    children = [str(x) for x in (rec.get("children") or []) if x] if rec else []
    return DimensionHierarchy(
        dimension_name=dim_name,
        parent=parents,
        children=children,
    )


def get_dimension_values(
    driver: Driver, domain: str, dim_name: str, datasource_id: str = "",
) -> list[str]:
    records, _ = get_dimension_value_records(
        driver, domain, dim_name, datasource_id=datasource_id,
    )
    return [r.value for r in records]


def get_dimension_value_records(
    driver: Driver,
    domain: str,
    dim_name: str,
    *,
    limit: int = 10,
    datasource_id: str = "",
) -> tuple[list[DimensionValueRecord], int]:
    """Return dimension enum values with a rollup-sentinel flag per row.

    Returns ``(records, total_count)`` — *records* is truncated to *limit*,
    *total_count* is the full number of values before truncation.

    Falls back to literal membership in ``TOPLINE_LITERALS_RICH`` when the
    graph node lacks an explicit ``is_rollup`` flag — keeps older imports
    where the supplement YAML hasn't yet been re-ingested usable.
    """
    get_dimension(driver, domain, dim_name, datasource_id)
    d_key = dim_key(domain, dim_name, datasource_id)
    ds = (datasource_id or "").strip()
    records: list[DimensionValueRecord] = []

    cypher_dv = f"""
    MATCH (d:Dimension {{key: $d_key}})-[:HAS_VALUE]->(dv:DimensionValue)
    WHERE {_domain_ds_clause("d")}
    RETURN dv.value AS value, coalesce(dv.occur_cnt, 0) AS cnt,
           coalesce(dv.is_rollup, false) AS is_rollup
    ORDER BY cnt DESC, value ASC
    LIMIT $limit
    """
    cypher_cnt = f"""
    MATCH (d:Dimension {{key: $d_key}})-[:HAS_VALUE]->(dv:DimensionValue)
    WHERE {_domain_ds_clause("d")}
    RETURN count(dv) AS total
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher_dv, d_key=d_key, limit=limit, ds=ds).data()
        total = 0
        if rows:
            cnt_rec = s.run(cypher_cnt, d_key=d_key, ds=ds).single()
            total = int(cnt_rec["total"]) if cnt_rec else 0
    for r in rows:
        v = r.get("value")
        if v is None or not str(v).strip():
            continue
        value_str = str(v)
        is_rollup = bool(r.get("is_rollup")) or value_str in TOPLINE_LITERALS_RICH
        records.append(DimensionValueRecord(value=value_str, is_rollup=is_rollup))

    if records:
        return records, total

    cypher_col = f"""
    MATCH (d:Dimension {{key: $d_key}})-[:MAPS_TO_COLUMN]->(col:Column)
    WHERE {_domain_ds_clause("d")}
    RETURN col.sample_values AS sv, col.value_mapping AS vm
    LIMIT 1
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher_col, d_key=d_key, ds=ds).single()
    if rec:
        raw = rec.get("sv") or rec.get("vm")
        candidates: list[str] = []
        if isinstance(raw, (list, tuple)):
            candidates = [str(v) for v in raw if v is not None and str(v).strip()]
        elif isinstance(raw, str) and raw.strip():
            candidates = [raw.strip()]
        for v in candidates:
            records.append(
                DimensionValueRecord(value=v, is_rollup=v in TOPLINE_LITERALS_RICH)
            )
    return records, len(records)


# ---------------------------------------------------------------------- #
# Datasets
# ---------------------------------------------------------------------- #

def list_datasets(
    driver: Driver, domain: str, *, datasource_id: str = "",
) -> list[Dataset]:
    require_domain(driver, domain, datasource_id)

    # 数据源隔离:所有 Dataset 节点通过 ``ds.datasource_id`` 属性路由,
    # 不再依赖名字前缀约定。未打标的旧节点在 ingest 阶段已由
    # ``_backfill_datasource_id`` 补齐,此处只做精确匹配。
    # 当 ``datasource_id`` 为空时返回该 domain 下全部 dataset(不过滤)。

    params: dict[str, Any] = {"domain": domain}
    if datasource_id:
        ds_filter = "AND ds.datasource_id = $datasource_id"
        params["datasource_id"] = datasource_id
    else:
        ds_filter = ""

    cypher = f"""
    MATCH (ds:Dataset)
    WHERE ds.domain = $domain
    {ds_filter}
    RETURN ds.name AS name,
           coalesce(ds.description, '') AS description,
           coalesce(ds.dataset_type, 'OLAP') AS dataset_type,
           coalesce(ds.sql, '') AS sql,
           coalesce(ds.parents, '') AS parents
    ORDER BY name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, **params).data()

    # 仅当该 domain **完全没有** Dataset 节点（pre-backfill 旧图）才回退到
    # Formula 派生。若 Dataset 节点存在但被 datasource 过滤空，说明该数据源下
    # 确实没有数据集，直接返回空（保证数据源隔离），不能再走 Metric 派生——
    # metric 是跨数据源共享的，按 metric 派生会泄漏到错误数据源。
    #
    # Fallback: 若当前 datasource_id 来自 synced default（与 domain default 不同），
    # 且该 domain 在 synced 数据源下无 dataset，回退到 domain default 重试。
    if not rows and datasource_id:
        with neo4j_session(driver) as s:
            has_ds_nodes = s.run(
                "MATCH (ds:Dataset) WHERE ds.domain = $domain RETURN count(*) AS c",
                domain=domain,
            ).single()
        if has_ds_nodes and int(has_ds_nodes["c"]) > 0:
            from .cm_resolve import default_datasource_for_domain
            domain_default = default_datasource_for_domain(domain)
            if domain_default and domain_default != datasource_id:
                log.info(
                    "list_datasets: synced datasource %r has no datasets for "
                    "domain %r, falling back to domain default %r",
                    datasource_id, domain, domain_default,
                )
                fb_filter = "AND ds.datasource_id = $fb_datasource_id"
                fb_cypher = f"""
                MATCH (ds:Dataset)
                WHERE ds.domain = $domain
                {fb_filter}
                RETURN ds.name AS name,
                       coalesce(ds.description, '') AS description,
                       coalesce(ds.dataset_type, 'OLAP') AS dataset_type,
                       coalesce(ds.sql, '') AS sql,
                       coalesce(ds.parents, '') AS parents
                ORDER BY name
                """
                try:
                    with neo4j_session(driver) as s:
                        rows = s.run(
                            fb_cypher, domain=domain,
                            fb_datasource_id=domain_default,
                        ).data()
                except Exception:
                    log.warning(
                        "list_datasets: domain-default fallback query failed",
                        exc_info=True,
                    )
                    return []
                if not rows:
                    return []
            else:
                return []

    if not rows:
        # fallback: distinct logical dataset names from formulas (pre-backfill graphs)
        from ..graph.keys import logical_dataset_name

        cypher2 = """
        MATCH (m:Metric {domain: $domain})-[:HAS_FORMULA]->(f:Formula)
        WHERE f.dataset IS NOT NULL AND f.dataset <> ''
        RETURN DISTINCT f.dataset AS raw
        ORDER BY raw
        """
        with neo4j_session(driver) as s:
            seen: set[str] = set()
            rows = []
            for r in s.run(cypher2, domain=domain).data():
                raw = str(r.get("raw") or "")
                if "." in raw:
                    logical = logical_dataset_name(raw)
                else:
                    logical = raw
                name = logical or raw
                if not name or name in seen:
                    continue
                seen.add(name)
                rows.append(
                    {
                        "name": name,
                        "description": "",
                        "dataset_type": "OLAP",
                        "sql": "",
                        "parents": name.removeprefix("view_"),
                    }
                )

    datasets = [
        Dataset(
            dataset_name=str(r.get("name") or ""),
            domain=domain,
            description=str(r.get("description") or ""),
            dataset_type=str(r.get("dataset_type") or "OLAP"),
            sql=str(r.get("sql") or ""),
            parents=str(r.get("parents") or ""),
        )
        for r in rows
        if r.get("name")
    ]
    return datasets


def _resolve_dataset_key(
    driver: Driver, domain: str, dataset_name: str, *, datasource_id: str = "",
) -> Optional[str]:
    from ..graph.keys import dataset_key as ds_key_fn, logical_dataset_name
    from .cm_resolve import default_datasource_for_domain

    candidates = [dataset_name.strip()]
    logical = logical_dataset_name(dataset_name)
    if logical and logical not in candidates:
        candidates.append(logical)
    expected_dsi = (datasource_id or "").strip() or default_datasource_for_domain(domain)
    # Key construction must use the RAW datasource_id (not the default-resolved
    # expected_dsi) so the key matches what the write path wrote.
    ds_id = (datasource_id or "").strip()
    with neo4j_session(driver) as s:
        for cand in candidates:
            if not cand:
                continue
            ds_key = ds_key_fn(domain, cand, ds_id)
            if s.run(
                "MATCH (ds:Dataset {key: $key}) RETURN ds LIMIT 1",
                key=ds_key,
            ).single():
                return ds_key
        rec = s.run(
            """
            MATCH (ds:Dataset {domain: $domain})
            WHERE ds.name = $name OR ds.parents = $name
               OR ds.qualified_table = $name
               OR $name ENDS WITH coalesce(ds.parents, '')
            RETURN ds.key AS key,
                   CASE WHEN ds.datasource_id = $expected_dsi THEN 0
                        WHEN ds.datasource_id IS NULL THEN 1
                        ELSE 2 END AS score
            ORDER BY score
            LIMIT 1
            """,
            domain=domain,
            name=dataset_name,
            expected_dsi=expected_dsi,
        ).single()
        if rec and rec.get("key"):
            return str(rec["key"])
    return None


def get_dataset_columns(
    driver: Driver, domain: str, dataset_name: str, *, datasource_id: str = "",
) -> list[ColumnMeta]:
    require_domain(driver, domain, datasource_id)
    ds_key = _resolve_dataset_key(driver, domain, dataset_name, datasource_id=datasource_id)
    if not ds_key:
        if not _dataset_tables_exist(driver, domain, dataset_name):
            raise KeyError(f"Dataset not found: {domain}::{dataset_name}")

    rows: list[dict] = []

    # Prefer DatasetColumn path
    if ds_key:
        dc_cypher = """
        MATCH (ds:Dataset {key: $ds_key})-[:HAS_COLUMN]->(dc:DatasetColumn)
        OPTIONAL MATCH (d:Dimension)-[:MAPS_TO_DATASET_COLUMN]->(dc)
        OPTIONAL MATCH (d)-[:HAS_VALUE]->(dv:DimensionValue)
        WITH dc, head(collect(DISTINCT d.name)) AS dim_name,
             collect(DISTINCT dv.value) AS _all_dim_vals
        RETURN dc.name AS name, dc.display_name AS display_name,
               dc.data_type AS type, dc.column_type AS explicit_column_type,
               coalesce(dc.description, '') AS comment,
               dc.granularity_role AS granularity_role,
               coalesce(dc.topline_value, '') AS topline_value,
               dc.sample_values AS sample_values,
               _all_dim_vals[..10] AS dim_vals,
               size(_all_dim_vals) AS dim_vals_total,
               dim_name,
               dc.composite AS composite,
               coalesce(dc.composite_desc, '') AS composite_desc
        ORDER BY name
        """
        with neo4j_session(driver) as s:
            rows = s.run(dc_cypher, ds_key=ds_key).data()

    # Fallback: legacy Column path
    if not rows and ds_key:
        legacy_cypher = """
        MATCH (ds:Dataset {key: $ds_key})-[:CONTAINS_TABLE]->(t:Table)-[:HAS_COLUMN]->(c:Column)
        OPTIONAL MATCH (d:Dimension)-[:MAPS_TO_COLUMN]->(c)
        OPTIONAL MATCH (d)-[:HAS_VALUE]->(dv:DimensionValue)
        WITH c, head(collect(DISTINCT d.name)) AS dim_name,
             collect(DISTINCT dv.value) AS _all_dim_vals
        RETURN c.name AS name, c.type AS type, coalesce(c.comment, '') AS comment,
               c.granularity_role AS granularity_role,
               coalesce(c.topline_value, '') AS topline_value,
               c.sample_values AS sample_values,
               _all_dim_vals[..10] AS dim_vals,
               size(_all_dim_vals) AS dim_vals_total,
               dim_name
        ORDER BY name
        """
        with neo4j_session(driver) as s:
            rows = s.run(legacy_cypher, ds_key=ds_key).data()

    if not rows:
        rows = _columns_by_dataset_name(driver, domain, dataset_name)

    if not rows:
        raise KeyError(f"Dataset not found: {domain}::{dataset_name}")

    return [_column_meta_from_row(r) for r in rows if r.get("name")]


def get_dataset_schema(
    driver: Driver, domain: str, dataset_name: str, *, datasource_id: str = "",
) -> DatasetSchema:
    cols = get_dataset_columns(driver, domain, dataset_name, datasource_id=datasource_id)
    # Fetch description / dataset_type from the Dataset node.
    ds_cypher = """
    MATCH (ds:Dataset {domain: $domain, name: $name})
    RETURN coalesce(ds.description, '') AS description,
           coalesce(ds.dataset_type, 'OLAP') AS dataset_type
    """
    with neo4j_session(driver) as s:
        ds_row = s.run(ds_cypher, domain=domain, name=dataset_name).single()
    desc = ds_row["description"] if ds_row else ""
    ds_type = ds_row["dataset_type"] if ds_row else "OLAP"
    return DatasetSchema(
        dataset_name=dataset_name,
        domain=domain,
        description=desc,
        dataset_type=ds_type,
        columns=cols,
    )


def _dataset_tables_exist(driver: Driver, domain: str, dataset_name: str) -> bool:
    return bool(_columns_by_dataset_name(driver, domain, dataset_name))


def _columns_by_dataset_name(driver: Driver, domain: str, dataset_name: str) -> list[dict]:
    """Resolve columns via Formula.dataset → Table when Dataset node is absent."""
    cypher = """
    MATCH (m:Metric {domain: $domain})-[:HAS_FORMULA]->(f:Formula)
    WHERE f.dataset = $dataset
    MATCH (f)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t:Table)-[:HAS_COLUMN]->(c:Column)
    OPTIONAL MATCH (d:Dimension)-[:MAPS_TO_COLUMN]->(c)
    OPTIONAL MATCH (d)-[:HAS_VALUE]->(dv:DimensionValue)
    WITH c, t, head(collect(DISTINCT d.name)) AS dim_name,
         collect(DISTINCT dv.value) AS _all_dim_vals
    RETURN DISTINCT c.name AS name, c.type AS type, coalesce(c.comment, '') AS comment,
           c.granularity_role AS granularity_role,
           coalesce(c.topline_value, '') AS topline_value,
           c.sample_values AS sample_values,
           _all_dim_vals[..10] AS dim_vals,
           size(_all_dim_vals) AS dim_vals_total,
           dim_name,
           t.name AS table_name
    ORDER BY name
    LIMIT 500
    """
    with neo4j_session(driver) as s:
        return s.run(cypher, domain=domain, dataset=dataset_name).data()


def _dataset_table_name(
    driver: Driver, domain: str, dataset_name: str, datasource_id: str = "",
) -> str:
    ds_key = _resolve_dataset_key(
        driver, domain, dataset_name, datasource_id=datasource_id,
    )
    if ds_key:
        cypher = """
        MATCH (ds:Dataset {key: $ds_key})-[:CONTAINS_TABLE]->(t:Table)
        RETURN t.name AS name LIMIT 1
        """
        with neo4j_session(driver) as s:
            rec = s.run(cypher, ds_key=ds_key).single()
        if rec and rec.get("name"):
            return str(rec["name"])
    rows = _columns_by_dataset_name(driver, domain, dataset_name)
    if rows and rows[0].get("table_name"):
        return str(rows[0]["table_name"])
    return dataset_name.removeprefix("view_") if dataset_name.startswith("view_") else dataset_name


def _extract_sample_values(r: dict[str, Any], limit: int = 10) -> tuple[list[str], int | None]:
    """Dimension values first, fallback to Column.sample_values.

    Returns ``(truncated_values, total_count)``.
    ``total_count`` is ``None`` when no values exist at all.
    """
    dim_vals = r.get("dim_vals") or []
    if dim_vals:
        vals = [str(v) for v in dim_vals if v is not None and str(v).strip()][:limit]
        total = r.get("dim_vals_total")
        return vals, int(total) if total is not None else len(vals)
    raw = r.get("sample_values")
    if isinstance(raw, (list, tuple)):
        all_vals = [str(v) for v in raw if v is not None and str(v).strip()]
        return all_vals[:limit], len(all_vals) if all_vals else None
    return [], None


def _column_meta_from_row(r: dict[str, Any]) -> ColumnMeta:
    explicit = str(r.get("explicit_column_type") or "").strip()
    if explicit in ("维度", "日期", "度量"):
        col_type = explicit
    else:
        col_props = {"type": r.get("type"), "granularity_role": r.get("granularity_role")}
        col_type = _column_type_from_props(col_props)
    desc = ""
    for field in ("dim_name", "display_name", "comment"):
        val = r.get(field)
        if val and str(val).strip():
            desc = str(val).strip()
            break
    sv, sv_total = _extract_sample_values(r)
    return ColumnMeta(
        column_name=str(r.get("name") or ""),
        column_type=col_type,
        data_type=str(r.get("type") or "string"),
        description=desc,
        granularity_role=str(r.get("granularity_role") or ""),
        topline_value=str(r.get("topline_value") or ""),
        sample_values=sv,
        sample_values_total=sv_total,
        composite=bool(r.get("composite")),
        composite_desc=str(r.get("composite_desc") or ""),
    )
