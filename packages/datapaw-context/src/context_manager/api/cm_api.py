"""CM API — unified Context Manager.

Router prefix: ``/api/v1/cm``
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from datapaw.context.blocking_io import BlockingIOError, BlockingPool
from datapaw.context.errors import ResourceBudgetExceeded

from ..config import CFG
from ..utils import neo4j_session
from . import semantic_store as sem_store
from .cm_models import (
    AmbiguityCandidate,
    ColumnMeta,
    DatasetListItem,

    DatasetSchema,
    DatasetSummary,
    DimensionDetail,
    DimensionHierarchy,
    DimensionSummary,
    DimensionValue,
    Domain,
    DomainOverview,
    DrillDimension,
    ExecuteSqlRequest,
    ExecuteSqlResponse,
    ExploreEntityAmbiguous,
    ExploreEntityHit,
    ExploreEntityRequest,
    MetricDetail,
    MetricDimensionBinding,
    MetricDimensionsResponse,
    DimensionMetricsResponse,
    MetricFormula,
    MetricSummary,
    MCPToolInfo,
    RecallExperienceRequest,
    RecallExperienceResponse,
    QueryRelevance,
    SearchContextRequest,
    SearchContextResponse,
    SearchEventRequest,
    SearchEventResponse,
    EventSearchHit,
    SimilarExperience,
    SourceColumn,
    TimeHints,
    CommonFilter,
)
from .cm_resolve import (
    anchor_domain_on_session,
    ambiguous_payload,
    default_datasource_for_domain,
    infer_domain,
    not_found_http,
    resolve_entity,
    resolve_entity_any,
    resolve_read_datasource,
    to_ambiguity_candidates,
)
from .ctx_session import ContextSession, SessionStore, make_session, make_snapshot
from .executor import execute_sql as topology_execute_sql
from .executor import execute_sql_async
from .cm_sql_results import (
    CM_LOCAL_BASE_URL as _CM_LOCAL_BASE_URL,
    SQL_DOWNLOAD_MAX_ROWS as _SQL_DOWNLOAD_MAX_ROWS,
    SQL_DOWNLOAD_TTL_SECONDS as _SQL_DOWNLOAD_TTL_SECONDS,
    SQL_PREVIEW_ROWS as _SQL_PREVIEW_ROWS,
    cleanup_expired_downloads as _cleanup_expired_downloads,
    save_sql_results_to_csv as _save_sql_results_to_csv,
    sql_cache as _sql_cache,
    sql_downloads_dir as _sql_downloads_dir,
)

log = logging.getLogger("api.cm_api")

router = APIRouter(prefix="/api/v1/cm", tags=["cm"])


def _to_dataset_name(raw: str) -> str:
    """Strip schema prefix and map to logical dataset name."""
    if "." in raw:
        from ..graph.keys import logical_dataset_name
        return logical_dataset_name(raw)
    return raw

def _driver(request: Request):
    return request.app.state.driver


def _store(request: Request) -> SessionStore:
    return request.app.state.session_store


def _new_session_ref(store: SessionStore) -> str:
    return f"ctx_{store.new_session_ref()}"


def _db_id_from_session(session: Optional[ContextSession]) -> str:
    """Derive primary_db_id from session.datasource_id via the registry."""
    if session is None:
        return ""
    ds_id = (session.datasource_id or "").strip()
    if not ds_id:
        return ""
    from ..graph.datasource_registry import try_resolve
    ds = try_resolve(ds_id)
    return ds.primary_db_id if ds else ""


def _resolve_db_id(
    *,
    db_id: Optional[str],
    session: Optional[ContextSession],
    driver: Any,
) -> str:
    if db_id:
        return db_id
    derived = _db_id_from_session(session)
    if derived:
        return derived
    try:
        from .retrieval import guess_physical_db_id
        return guess_physical_db_id(driver) or "app_db"
    except Exception:
        return "app_db"


def _get_session_optional(
    session_ref: Optional[str],
    store: SessionStore,
) -> Optional[ContextSession]:
    if not session_ref:
        return None
    session = store.get(session_ref)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"session_ref '{session_ref}' not found or expired",
        )
    return session


# _default_datasource_for_domain 已迁移到 cm_resolve.default_datasource_for_domain
# 此处保留别名以兼容旧代码
_default_datasource_for_domain = default_datasource_for_domain


_FROM_TABLE_RE = re.compile(
    r"\bfrom\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)"
    r"|\bjoin\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)",
    flags=re.IGNORECASE,
)


def _extract_sql_tables(sql: str) -> list[str]:
    """Extract table names referenced in FROM / JOIN clauses.

    Strips schema prefixes (``schema.table`` → ``table``) and deduplicates
    while preserving first-seen order.
    """
    raw: list[str] = []
    for m in _FROM_TABLE_RE.finditer(sql):
        name = m.group(1) or m.group(2)
        if not name:
            continue
        # Drop schema prefix if present
        if "." in name:
            name = name.rsplit(".", 1)[1]
        raw.append(name)
    seen: set[str] = set()
    out: list[str] = []
    for n in raw:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _any_table_on_odps(driver: Any, domain: str, table_names: list[str]) -> bool:
    """Return True if any of ``table_names`` resolves to an ODPS Dataset.

    Queries the graph for Dataset nodes whose ``datasource_id = 'analytics_dw'``
    and whose name / parents / qualified_table matches one of the candidates.
    """
    if not driver or not table_names:
        return False
    try:
        with neo4j_session(driver) as s:
            rec = s.run(
                """
                MATCH (ds:Dataset)
                WHERE ds.datasource_id = $odps_ds
                  AND (ds.name IN $names OR ds.parents IN $names
                       OR ds.qualified_table IN $names)
                RETURN ds.name LIMIT 1
                """,
                odps_ds="analytics_dw",
                names=table_names,
            ).single()
            return rec is not None
    except Exception as exc:
        log.debug("ODPS table probe failed: %s", exc)
        return False


def _effective_datasource_id(
    req_datasource_id: Optional[str],
    session: ContextSession,
    sql: str = "",
    *,
    driver: Any = None,
    domain: str = "",
) -> str:
    """SQL 执行时解析连库用的 datasource 标识：请求显式值 > synced default > 图路由。

    连库凭证由 semantic_config.db 按真实 ``datasource_id``/数字 ``id`` 查，
    故这里返回真实标识，不再映射成图谱 canonical ID（appdata/analytics_dw）——
    canonicalize 只服务于图谱作用域。

    - 请求显式值 / synced default：原样返回真实 id。
    - 图路由：从 SQL 的 FROM/JOIN 抽出表名，查 Dataset 节点的 ``datasource_id``
      属性判定是否走 ODPS；命中时把 canonical ``analytics_dw`` 回落到
      semantic_config.db 中 odps 类型的真实 datasource_id。
    """
    ds = (req_datasource_id or "").strip()
    if ds:
        return ds
    from .datasource_active_api import get_synced_default_datasource_id
    synced = get_synced_default_datasource_id()
    if synced:
        return synced
    if sql and driver:
        tables = _extract_sql_tables(sql)
        if tables and _any_table_on_odps(driver, domain, tables):
            from .datasource_active_api import resolve_datasource_id_by_canonical
            return resolve_datasource_id_by_canonical("analytics_dw") or "analytics_dw"
    return ""


def _ensure_session(
    request: Request,
    session_ref: Optional[str],
    *,
    scope: Optional[dict[str, Any]] = None,
    original_query: str = "",
    datasource_id: Optional[str] = None,
) -> tuple[ContextSession, str]:
    """Return (session, session_ref); create implicit session if needed."""
    store = _store(request)
    ds_id = (datasource_id or "").strip()

    if session_ref:
        session = _get_session_optional(session_ref, store)
        assert session is not None
        if ds_id:
            session.datasource_id = ds_id
        return session, session_ref

    ref = _new_session_ref(store)
    session = make_session(
        session_ref=ref,
        scope=dict(scope or {}),
        original_query=original_query,
        datasource_id=ds_id,
    )
    store.put(session)
    return session, ref


def _domain_from_request(
    request: Request,
    *,
    domain: Optional[str],
    session_ref: Optional[str],
) -> tuple[str, Optional[ContextSession]]:
    store = _store(request)
    driver = _driver(request)
    session = _get_session_optional(session_ref, store) if session_ref else None
    dom = infer_domain(driver, domain_param=domain, session=session, store_sess=store)
    if session:
        anchor_domain_on_session(session, dom)
        store.put(session)
    return dom, session


def _read_datasource_id(domain: str, session: Optional[ContextSession], datasource_id: str = "") -> str:
    """read 接口过滤用的 datasource_id：只认请求显式值，缺失即不过滤。"""
    return resolve_read_datasource(domain, req_datasource_id=datasource_id)


# ---------------------------------------------------------------------- #
# L3 helpers
# ---------------------------------------------------------------------- #

def _domain_model(d: Any) -> Domain:
    return Domain(
        name=d.name,
        display_name=d.display_name or d.name,
        description=d.description,
        aliases=list(d.aliases or []),
        datasource_id=getattr(d, "datasource_id", "") or "",
    )


def _list_metric_summaries_cm(
    driver: Any, domain: str, datasource_id: str = "",
) -> list[MetricSummary]:
    ds = (datasource_id or "").strip()
    cypher = """
    MATCH (dom:Domain {name: $domain})-[:HAS_METRIC]->(m:Metric)
    WHERE (m.valid_to IS NULL OR m.valid_to > datetime())
      AND ($ds = '' OR dom.datasource_id = $ds)
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
    from ..graph.semantic_fields import metric_role_from_props

    with neo4j_session(driver) as s:
        rows = s.run(cypher, domain=domain, ds=ds).data()
    out: list[MetricSummary] = []
    for r in rows:
        if not r.get("name"):
            continue
        out.append(
            MetricSummary(
                metric_name=str(r["name"]),
                description=str(r.get("description") or ""),
                aliases=sem_store._str_list(r.get("aliases")),
                tags=[str(x) for x in (r.get("tags") or []) if x],
                role=metric_role_from_props(r),
            )
        )
    return out


def _list_dimension_summaries_cm(
    driver: Any, domain: str, datasource_id: str = "",
) -> list[DimensionSummary]:
    ds = (datasource_id or "").strip()
    cypher = """
    MATCH (d:Dimension {domain: $domain})
    WHERE ($ds = '' OR d.datasource_id = $ds)
    RETURN d.name AS name,
           coalesce(d.aliases, []) AS aliases,
           coalesce(d.dimension_type, 'OLAP维度') AS dimension_type,
           coalesce(d.is_display_dimension, true) AS is_display_dimension
    ORDER BY name
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, domain=domain, ds=ds).data()
    return [
        DimensionSummary(
            dimension_name=str(r["name"]),
            aliases=sem_store._str_list(r.get("aliases")),
            dimension_type=str(r.get("dimension_type") or "OLAP维度"),
            is_display_dimension=bool(r.get("is_display_dimension", True)),
        )
        for r in rows
        if r.get("name")
    ]


def _metric_detail_cm(
    driver: Any, domain: str, metric_name: str, datasource_id: str = "",
) -> MetricDetail:
    ds = (datasource_id or "").strip()
    detail = sem_store.get_metric_detail(
        driver, domain, metric_name, datasource_id=ds,
    )
    formula_semantic = ""
    if detail.formulas:
        formula_semantic = detail.formulas[0].formula or ""
    source_columns: list[SourceColumn] = []
    common_filters: list[CommonFilter] = []
    try:
        from .retrieval import expand_subgraph
        from ..graph.keys import logical_dataset_name, metric_key

        sg = expand_subgraph(driver, metric_key(domain, metric_name))
        raw = sg.get("raw") or {}
        col_node_by_key: dict[str, dict] = {
            str(n.get("id") or ""): n
            for n in (sg.get("nodes") or [])
            if isinstance(n, dict) and n.get("group") == "Column" and n.get("id")
        }
        dim_node_by_key: dict[str, dict] = {
            str(n.get("id") or ""): n
            for n in (sg.get("nodes") or [])
            if isinstance(n, dict) and n.get("group") == "Dimension" and n.get("id")
        }
        col_to_dim: dict[str, str] = {}
        for e in sg.get("edges") or []:
            if isinstance(e, dict) and e.get("label") == "MAPS_TO_COLUMN":
                src = str(e.get("source") or "")
                tgt = str(e.get("target") or "")
                if src in dim_node_by_key and tgt in col_node_by_key:
                    dprops = dim_node_by_key[src].get("props") or {}
                    dname = str(dprops.get("name") or "")
                    if dname:
                        col_to_dim[tgt] = dname
        seen_source: set[tuple[str, str]] = set()
        for f in raw.get("formulas") or []:
            expr = str(f.get("expression") or f.get("formula") or "")
            if expr and not formula_semantic:
                formula_semantic = expr
            ds = str(f.get("dataset") or "")
            if "." in ds:
                ds = logical_dataset_name(ds)
            for uc in f.get("uses_columns") or []:
                if not isinstance(uc, dict):
                    continue
                ckey = str(uc.get("key") or "")
                node = col_node_by_key.get(ckey)
                if not node:
                    continue
                props = node.get("props") or {}
                phys_name = str(props.get("name") or "")
                if not phys_name:
                    continue
                sem_name = col_to_dim.get(ckey) or str(props.get("comment") or "") or phys_name
                dedup = (ckey, ds)
                if dedup in seen_source:
                    continue
                seen_source.add(dedup)
                source_columns.append(
                    SourceColumn(
                        name=sem_name,
                        dataset=ds,
                        role=str(uc.get("role") or "measure"),
                        granularity_role=str(props.get("granularity_role") or ""),
                        topline_value=str(props.get("topline_value") or ""),
                    )
                )
        for cal in raw.get("calibers") or []:
            props = cal.get("props") or cal.get("cal") or {}
            if isinstance(props, dict):
                fe = str(props.get("filter_expr") or "")
                desc = str(props.get("description") or fe)
                if fe:
                    common_filters.append(
                        CommonFilter(description=desc, sql_fragment=fe)
                    )
    except Exception as exc:
        log.debug("metric detail enrich failed: %s", exc)

    return MetricDetail(
        metric_name=detail.metric_name,
        domain=detail.domain,
        description=detail.description,
        unit=detail.unit,
        aliases=list(detail.aliases),
        tags=list(detail.tags),
        role=detail.role,
        formula_semantic=formula_semantic,
        formulas=[
            MetricFormula(
                dataset=_to_dataset_name(f.dataset),
                formula=f.formula,
                formula_evidence=f.formula_evidence,
                date_range=f.date_range,
            )
            for f in detail.formulas
        ],
        anomaly_rules=list(detail.anomaly_rules),
        dimensions=[
            MetricDimensionBinding(
                dimension_name=b.dimension_name,
                is_display_dimension=b.is_display_dimension,
                is_contribution_dimension=b.is_contribution_dimension,
            )
            for b in detail.dimensions
        ],
        source_columns=source_columns[:20],
        common_filters=common_filters[:10],
        related_metrics=[],
        related_knowledge=[],
    )


def _dimension_detail_cm(
    driver: Any, domain: str, dim_name: str, datasource_id: str = "",
) -> DimensionDetail:
    ds = (datasource_id or "").strip()
    d = sem_store.get_dimension(driver, domain, dim_name, datasource_id=ds)
    sample_values: list[DimensionValue] = []
    records, sv_total = sem_store.get_dimension_value_records(
        driver, domain, dim_name, datasource_id=ds,
    )
    for i, rec in enumerate(records[:20]):
        sample_values.append(
            DimensionValue(
                value=rec.value,
                business_meaning="",
                frequency=max(0.0, 0.35 - i * 0.02),
                is_rollup_sentinel=rec.is_rollup,
            )
        )
    return DimensionDetail(
        dimension_name=d.dimension_name,
        domain=d.domain,
        dataset_name=d.dataset_name,
        calculate_expr=d.calculate_expr,
        dimension_type=d.dimension_type,
        data_type=d.data_type,
        aliases=list(d.aliases),
        parent_dimension=d.parent_dimension,
        hierarchy_level=d.hierarchy_level,
        is_display_dimension=d.is_display_dimension,
        is_contribution_dimension=d.is_contribution_dimension,
        sample_values=sample_values,
        sample_values_total=sv_total,
        related_knowledge=[],
    )


def _build_domain_overview(
    driver: Any, domain: str, datasource_id: str = "",
) -> DomainOverview:
    from .datasource_active_api import resolve_datasource_id
    ds_id = (datasource_id or "").strip() or resolve_datasource_id(
        default_datasource_for_domain(domain)
    )
    domains = sem_store.list_domain_records(driver, datasource_id=ds_id)
    dom_rec = next((d for d in domains if d.name == domain), None)
    if not dom_rec:
        # Fallback: search across all datasources if scoped lookup misses
        # (e.g. domain exists but under a different datasource_id).
        domains = sem_store.list_domain_records(driver)
        dom_rec = next((d for d in domains if d.name == domain), None)
    if not dom_rec:
        raise HTTPException(status_code=404, detail=f"Domain not found: {domain}")
    metrics = _list_metric_summaries_cm(driver, domain, datasource_id=ds_id)
    north = [m for m in metrics if m.role == "north_star"]
    if not north:
        try:
            ns = sem_store.list_north_star_metrics(driver, domain, datasource_id=ds_id)
            north = [
                MetricSummary(
                    metric_name=m.metric_name,
                    description=m.description,
                    aliases=list(m.aliases),
                    tags=list(m.tags),
                    role="north_star",
                )
                for m in ns
            ]
        except Exception:
            pass
    dims = _list_dimension_summaries_cm(driver, domain, datasource_id=ds_id)
    # metric/dimension/dataset 全部按 datasource_id 过滤（Tasks 7-9 后语义层已隔离）。
    datasets_raw = sem_store.list_datasets(driver, domain, datasource_id=ds_id)
    datasets = [
        DatasetSummary(
            dataset_name=ds.dataset_name,
            description=ds.description,
            dataset_type=ds.dataset_type,
        )
        for ds in datasets_raw
    ]
    top_dims = [d.dimension_name for d in dims if d.is_display_dimension][:10]
    return DomainOverview(
        domain=_domain_model(dom_rec),
        north_star_metrics=north,
        metric_count=len(metrics),
        dimension_count=len(dims),
        dataset_count=len(datasets),
        top_dimensions=top_dims,
        datasets=datasets,
    )



# ---------------------------------------------------------------------- #
# MCP tool discovery (REST)
# ---------------------------------------------------------------------- #


@router.get("/mcp/tools", response_model=list[MCPToolInfo])
async def list_cm_mcp_tools() -> list[MCPToolInfo]:
    """List MCP tools from the embedded CM server (plain JSON, no JSON-RPC/SSE)."""
    from context_manager.mcp.cm_server import mcp as cm_mcp

    try:
        tools = await cm_mcp.list_tools()
    except Exception as exc:
        log.exception("list_cm_mcp_tools: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [
        MCPToolInfo(
            name=t.name,
            description=getattr(t, "description", "") or "",
            input_schema=getattr(t, "inputSchema", None) or {},
        )
        for t in tools
    ]


# ---------------------------------------------------------------------- #
# L3 — GET endpoints
# ---------------------------------------------------------------------- #

@router.get("/domains", response_model=list[Domain])
def cm_list_domains(
    request: Request,
    datasource_id: Optional[str] = Query(None),
) -> list[Domain]:
    driver = _driver(request)
    ds = (datasource_id or "").strip()
    try:
        return [_domain_model(d) for d in sem_store.list_domain_records(driver, datasource_id=ds)]
    except Exception as exc:
        log.exception("list_domains: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/domain-overview", response_model=DomainOverview)
def cm_domain_overview(
    request: Request,
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> DomainOverview:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    try:
        return _build_domain_overview(driver, dom, _read_datasource_id(dom, sess, datasource_id or ""))
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/metrics")
def cm_metrics(
    request: Request,
    domain: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> Union[list[MetricSummary], MetricDetail, dict[str, Any]]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    if not (name or "").strip():
        try:
            return _list_metric_summaries_cm(driver, dom, datasource_id=ds_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    resolved = resolve_entity(
        driver, domain=dom, name=name.strip(), kind="metric", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(
                resolved.candidates,
                entity_type="Metric",
            ),
        )
    if resolved.kind == "not_found":
        raise not_found_http("metric", dom, name)
    try:
        return _metric_detail_cm(driver, dom, resolved.canonical_name, datasource_id=ds_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/north-star-metrics", response_model=list[MetricSummary])
def cm_north_star_metrics(
    request: Request,
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> list[MetricSummary]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    try:
        rows = sem_store.list_north_star_metrics(driver, dom, datasource_id=ds_id)
        return [
            MetricSummary(
                metric_name=m.metric_name,
                description=m.description,
                aliases=list(m.aliases),
                tags=list(m.tags),
                role="north_star",
            )
            for m in rows
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/search-metrics", response_model=list[MetricSummary])
def cm_search_metrics(
    request: Request,
    query: str = Query(..., description="自然语言查询（支持 name/aliases/tags 语义匹配）"),
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    k: int = Query(10, ge=1, le=50, description="最大返回条数"),
    relevance_threshold: Optional[float] = Query(
        None, ge=0.0, le=1.0,
        description="相关度门槛。低于此值时返回结构化错误响应（含 status/message/suggestion）。不传时使用服务端默认值 (0.40)。",
    ),
    datasource_id: Optional[str] = Query(None),
) -> Union[list[MetricSummary], JSONResponse]:
    """语义检索指标。

    内部复用 ``retrieval.resolve_metric`` 的混合检索路径
    （fulltext + vector + RRF），对 query 做语义匹配后按 domain 过滤。

    检索流程：
    1. 精确检索（全文索引）
    2. 混合检索（RRF 融合全文 + 向量）
    3. 相似度门槛过滤（默认 0.40）

    未达到门槛时返回结构化错误响应（含 status/message/suggestion），
    而非空数组，以便调用方区分"无结果"与"检索未命中"。
    """
    from .retrieval import resolve_metric, relevance_gate

    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    if not (query or "").strip():
        raise HTTPException(status_code=422, detail="query is required")
    rows = resolve_metric(driver, query.strip(), k=k, domain=dom, datasource_id=ds_id)

    filtered, gate = relevance_gate(
        rows, query.strip(), threshold=relevance_threshold,
        name_field="name", score_field="score",
    )

    if gate["status"] in ("no_match", "low_confidence"):
        status = gate["status"]
        log.info(f"[search-metrics] {status}: query={query}, score={gate['score']:.3f}")
        if status == "no_match":
            msg = f"未检索到与「{query}」相关的指标（相关度 {gate['score']:.2f}，低于门槛）。"
        else:
            msg = f"检索到与「{query}」弱相关的指标（相关度 {gate['score']:.2f}），但置信度不足，已过滤。"
        suggestion = (
            "建议：1) 换用更通用的关键词重试；"
            "2) 调用 list_metrics 查看当前 domain 的全量指标列表确认是否存在；"
            "3) 确认 domain 参数是否正确。"
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": status,
                "message": msg,
                "suggestion": suggestion,
                "score": gate["score"],
                "matched_name": gate.get("matched_name", ""),
            },
        )

    from ..graph.semantic_fields import metric_role_from_props

    return [
        MetricSummary(
            metric_name=str(r.get("name") or ""),
            description=str(r.get("description") or ""),
            aliases=sem_store._str_list(r.get("aliases")),
            tags=[str(x) for x in (r.get("tags") or []) if x],
            role=metric_role_from_props(r),
        )
        for r in filtered
        if r.get("name")
    ]


@router.get("/dimensions")
def cm_dimensions(
    request: Request,
    domain: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> Union[list[DimensionSummary], DimensionDetail, dict[str, Any]]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    if not (name or "").strip():
        try:
            return _list_dimension_summaries_cm(driver, dom, datasource_id=ds_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    resolved = resolve_entity(
        driver, domain=dom, name=name.strip(), kind="dimension", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Dimension"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("dimension", dom, name)
    try:
        return _dimension_detail_cm(driver, dom, resolved.canonical_name, datasource_id=ds_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/dimension-hierarchy", response_model=DimensionHierarchy)
def cm_dimension_hierarchy(
    request: Request,
    name: str = Query(...),
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> DimensionHierarchy:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    resolved = resolve_entity(
        driver, domain=dom, name=name, kind="dimension", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Dimension"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("dimension", dom, name)
    try:
        h = sem_store.get_dimension_hierarchy(
            driver, dom, resolved.canonical_name, datasource_id=ds_id,
        )
        return DimensionHierarchy(
            dimension_name=h.dimension_name,
            parent=list(h.parent),
            children=list(h.children),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/dimension-values", response_model=list[DimensionValue])
def cm_dimension_values(
    request: Request,
    name: str = Query(...),
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=500),
    datasource_id: Optional[str] = Query(None),
) -> list[DimensionValue]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    resolved = resolve_entity(
        driver, domain=dom, name=name, kind="dimension", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Dimension"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("dimension", dom, name)
    records, _ = sem_store.get_dimension_value_records(
        driver, dom, resolved.canonical_name, limit=limit, datasource_id=ds_id,
    )
    return [
        DimensionValue(
            value=rec.value,
            business_meaning="",
            frequency=max(0.0, 0.4 - i * 0.03),
            is_rollup_sentinel=rec.is_rollup,
        )
        for i, rec in enumerate(records)
    ]


@router.get("/metric-dimensions")
def cm_metric_dimensions(
    request: Request,
    name: str = Query(...),
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> Union[MetricDimensionsResponse, dict[str, Any]]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    resolved = resolve_entity(
        driver, domain=dom, name=name, kind="metric", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Metric"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("metric", dom, name)
    bindings = sem_store.get_dimensions_for_metric(
        driver, dom, resolved.canonical_name,
        metric_key_actual=resolved.graph_key or None,
        datasource_id=ds_id,
    )
    return MetricDimensionsResponse(
        metric_name=resolved.canonical_name,
        domain=dom,
        dimensions=[
            MetricDimensionBinding(
                dimension_name=b.dimension_name,
                is_display_dimension=b.is_display_dimension,
                is_contribution_dimension=b.is_contribution_dimension,
            )
            for b in bindings
        ],
    )


@router.get("/dimension-metrics")
def cm_dimension_metrics(
    request: Request,
    name: str = Query(...),
    domain: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> Union[DimensionMetricsResponse, dict[str, Any]]:
    """反向查询：给定维度，返回所有可通过该维度分析的指标。"""
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    resolved = resolve_entity(
        driver, domain=dom, name=name, kind="dimension", datasource_id=ds_id,
    )
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Dimension"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("dimension", dom, name)
    metrics = sem_store.get_metrics_for_dimension(
        driver, dom, resolved.canonical_name, datasource_id=ds_id,
    )
    return DimensionMetricsResponse(
        dimension_name=resolved.canonical_name,
        domain=dom,
        metrics=[
            MetricSummary(
                metric_name=m.metric_name,
                description=m.description,
                aliases=list(m.aliases),
                tags=list(m.tags),
                role=m.role,
            )
            for m in metrics
        ],
    )


@router.get("/datasets")
def cm_datasets(
    request: Request,
    domain: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    session_ref: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
) -> Union[list[DatasetListItem], DatasetSchema, dict[str, Any]]:
    driver = _driver(request)
    dom, sess = _domain_from_request(request, domain=domain, session_ref=session_ref)
    ds_id = _read_datasource_id(dom, sess, datasource_id or "")
    if not (name or "").strip():
        try:
            raw = sem_store.list_datasets(driver, dom, datasource_id=ds_id)
            return [
                DatasetListItem(
                    dataset_name=ds.dataset_name,
                    domain=dom,
                    description=ds.description,
                    dataset_type=ds.dataset_type,
                )
                for ds in raw
            ]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    resolved = resolve_entity(driver, domain=dom, name=name.strip(), kind="dataset", datasource_id=ds_id)
    if resolved.kind == "ambiguous":
        return JSONResponse(
            status_code=200,
            content=ambiguous_payload(resolved.candidates, entity_type="Dataset"),
        )
    if resolved.kind == "not_found":
        raise not_found_http("dataset", dom, name)
    try:
        schema = sem_store.get_dataset_schema(driver, dom, resolved.canonical_name, datasource_id=ds_id)
        return DatasetSchema(
            dataset_name=schema.dataset_name,
            domain=schema.domain,
            description=schema.description,
            dataset_type=schema.dataset_type,
            columns=[
                ColumnMeta(
                    column_name=c.column_name,
                    column_type=c.column_type,
                    data_type=c.data_type,
                    description=c.description,
                    granularity_role=c.granularity_role,
                    topline_value=c.topline_value,
                    sample_values=c.sample_values,
                    sample_values_total=c.sample_values_total,
                    composite=c.composite,
                    composite_desc=c.composite_desc,
                )
                for c in schema.columns
            ],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# L1 — search_context
# ---------------------------------------------------------------------- #

def _build_schema_prompt(pack_semantics: Any) -> str:
    """Build a natural-language semantic brief from SemanticsBlock.

    Organised around metrics → their formulas / columns / dimensions →
    business rules.  Physical structure (table names, column types) is
    woven into the descriptions, not listed as standalone DDL.
    """
    sections: list[str] = []

    # ── Metrics ──────────────────────────────────────────────────────── #
    metrics = getattr(pack_semantics, "metrics", []) or []
    columns = getattr(pack_semantics, "columns", []) or []
    dimensions = getattr(pack_semantics, "dimensions", []) or []
    business_rules = getattr(pack_semantics, "business_rules", []) or []
    knowledge = getattr(pack_semantics, "knowledge", []) or []

    if metrics:
        for m in metrics:
            parts: list[str] = [f"指标：{m.name}"]
            desc = getattr(m, "description", "") or ""
            unit = getattr(m, "unit", "") or ""
            domain = getattr(m, "domain", "") or ""
            aliases = getattr(m, "aliases", []) or []
            formula_ev = getattr(m, "formula_evidence", "") or ""
            definition = getattr(m, "definition", "") or ""

            if domain:
                parts.append(f"  业务域：{domain}")
            if desc:
                parts.append(f"  含义：{desc}")
            elif aliases:
                parts.append(f"  含义：{aliases[0]}")
            if unit:
                parts.append(f"  单位：{unit}")

            # Formula / definition — prefer evidence over raw expression
            if formula_ev:
                parts.append(f"  口径：{formula_ev}")
            elif definition:
                parts.append(f"  口径：{definition}")

            # Associate columns with this metric's table(s)
            tables = getattr(pack_semantics, "tables", []) or []
            table_names = {getattr(t, "name", "") for t in tables}
            if table_names:
                # When tables are known, only show their columns
                related_cols = [
                    c for c in columns
                    if getattr(c, "table", "") in table_names
                ]
            else:
                # When no table info, show all columns (especially those with
                # granularity_role or topline_value — these are critical hints)
                related_cols = columns
            if related_cols:
                col_descs: list[str] = []
                for c in related_cols:
                    gr = getattr(c, "granularity_role", "") or ""
                    tv = getattr(c, "topline_value", "") or ""
                    dt = getattr(c, "data_type", "") or ""
                    cmt = getattr(c, "comment", "") or ""
                    svs = getattr(c, "sample_values", []) or []
                    line = f"  · {c.name}"
                    if dt:
                        line += f"（{dt}）"
                    if cmt:
                        line += f" —— {cmt}"
                    # Use role= prefix for granularity_role tags
                    if gr:
                        line += f" [role={gr}]"
                    if tv:
                        line += f" [topline='{tv}']；含汇总行 '{tv}'，按维度拆分时需排除"
                    elif svs:
                        sv_str = "、".join(str(v) for v in svs[:4])
                        line += f"；取值如：{sv_str}"
                    col_descs.append(line)
                parts.append("  相关列：")
                parts.extend(col_descs)

            sections.append("\n".join(parts))

    # ── Dimensions ───────────────────────────────────────────────────── #
    if dimensions:
        dim_lines: list[str] = ["可用分析维度："]
        for d in dimensions:
            line = f"  · {d.name}"
            aliases = getattr(d, "aliases", []) or []
            svs = getattr(d, "sample_values", []) or []
            if aliases:
                line += f"（别名：{', '.join(aliases[:3])}）"
            if svs:
                sv_str = "、".join(str(v) for v in svs[:5])
                line += f"；取值如：{sv_str}"
            dim_lines.append(line)
        sections.append("\n".join(dim_lines))

    # ── Business Rules ───────────────────────────────────────────────── #
    if business_rules:
        rule_lines = ["业务规则："]
        for r in business_rules:
            rule_lines.append(f"  · {r}")
        sections.append("\n".join(rule_lines))

    # ── Knowledge ────────────────────────────────────────────────────── #
    if knowledge:
        kn_lines = ["相关知识："]
        for kn in knowledge:
            label = getattr(kn, "label", "") or ""
            name = getattr(kn, "name", "") or ""
            summary = getattr(kn, "summary", "") or ""
            entry = f"  · [{label}] {name}"
            if summary:
                entry += f"：{summary[:200]}"
            kn_lines.append(entry)
        sections.append("\n".join(kn_lines))

    return "\n\n".join(sections) if sections else ""


def _build_path_hint(pack: Any, domain: str) -> str:
    metrics = getattr(pack.semantics, "metrics", []) or []
    tables = getattr(pack.semantics, "tables", []) or []
    parts: list[str] = []
    if domain:
        parts.append(f"已锚定业务域 {domain}。")
    if metrics:
        names = ", ".join(m.name for m in metrics[:5])
        parts.append(f"匹配指标：{names}。")
    if tables:
        parts.append(f"相关表：{', '.join(t.name for t in tables[:5])}。")
    return " ".join(parts) if parts else ""


def _similar_experiences_from_cards(cards: list[Any]) -> list[SimilarExperience]:
    out: list[SimilarExperience] = []
    for c in cards[:5]:
        lesson = str(getattr(c, "lesson", "") or getattr(c, "strategy_semantics", "") or "")
        if not lesson:
            continue
        out.append(
            SimilarExperience(
                question=str(getattr(c, "strategy_semantics", "") or ""),
                lesson=lesson[:300],
                similarity=float(getattr(c, "composite_score", 0.0) or 0.0),
            )
        )
    return out


def _compute_relevance(
    anchors: Any,
    query: str = "",
    threshold: Optional[float] = None,
    domain: str = "",
) -> QueryRelevance:
    """Compute overall query relevance from anchor retrieval.

    Blends a CJK-tolerant soft text match (name/aliases/description) with the
    *raw vector cosine* (rescaled from Neo4j's ``(1+cos)/2`` range). The dense
    cosine — not the rank-only RRF score — is the relevance signal, so semantic
    paraphrases like "访问趋势分析" can hit metric "DAU"/alias "访问用户数" even
    with no lexical overlap. See ``runtime/relevance.py`` for the rationale.

    Args:
        anchors: AnchorSet from pipeline
        query: original user question
        threshold: User-provided threshold (default ``CFG.relevance_threshold``)
    """
    from ..runtime.relevance import classify
    from .semantic_pack import score_anchor

    best_score = 0.0
    best_anchor_name = ""

    if query:
        for bucket_attr in ("anchors_metric", "anchors_dimension"):
            for a in getattr(anchors, bucket_attr, []) or []:
                # Use score_anchor (not raw score_candidate) so the overall
                # relevance verdict honors the precision-rerank score, keeping it
                # consistent with the primary/alternative ordering in shape_l1.
                blended = score_anchor(query, a, domain=domain)
                if blended > best_score:
                    best_score = blended
                    best_anchor_name = getattr(a, "name", "")

    status = classify(best_score, threshold=threshold)
    if status == "relevant":
        detail = f"匹配到「{best_anchor_name}」" if best_anchor_name else ""
    elif status == "low_confidence":
        detail = "匹配度较低，可能没有对应数据"
    else:
        detail = "未找到与该问题相关的数据"

    return QueryRelevance(
        status=status,
        score=round(best_score, 3),
        detail=detail,
    )


def _finish_search_context_from_pipe(
    *,
    req: SearchContextRequest,
    session: Any,
    session_ref: str,
    store: Any,
    pipe: dict[str, Any],
    scope_dict: dict[str, Any],
) -> SearchContextResponse:
    """Run snapshot assembly + L1 response mapping from pipeline output."""
    from .ctx_assemble import assemble_context_pack

    for a in getattr(pipe["anchors"], "anchors_metric", []) or []:
        d = getattr(a, "domain", "") or ""
        if d:
            anchor_domain_on_session(session, d)
            break

    snap = make_snapshot(
        trigger="search_context",
        query=req.query,
        anchors=pipe["anchors"],
        subgraph=pipe["subgraph"],
        decision=pipe["decision"],
        cards_visible=pipe["cards_visible"],
        cards_blocked=pipe["cards_blocked"],
        top_card_gate=pipe["top_card_gate"],
        facets=pipe["facets"],
        parent_id=session.current.snapshot_id if session.snapshots else None,
        expanded_subgraphs=pipe["expanded_subgraphs"],
        store=store,
    )
    session.append_snapshot(snap)
    store.put(session)

    pack = assemble_context_pack(
        session_ref,
        session.snapshot_index,
        snap,
        include_debug=req.include_debug,
        pipeline_steps=pipe.get("pipeline_steps") if req.include_operation else None,
        endpoint="search_context",
    )

    # ── L1 shaping: primary + alternatives + knowledge notes + metric-centric schema_prompt ──
    from .l1_shape import shape_l1

    # Get domain from session scope (needed for L1 shaping, path_hint, and ambiguity)
    domain = (session.scope or {}).get("domain") or ""

    primary_metrics, alternative_metrics, knowledge_notes, schema_prompt = shape_l1(
        req.query, pipe["anchors"], pipe.get("expanded_subgraphs", {}),
        domain=domain,
    )

    # True ambiguity: multiple primary metrics with similar relevance
    amb_candidates: list[AmbiguityCandidate] = []
    ambiguous = False
    if len(primary_metrics) >= 2:
        ambiguous = True
        for c in primary_metrics:
            amb_candidates.append(
                AmbiguityCandidate(
                    entity_type="Metric",
                    name=c.metric_name,
                    domain=domain,
                    description=c.description,
                    match_confidence=c.relevance_score,
                    disambiguation_hint="",
                )
            )

    time_list = list(getattr(pipe["anchors"], "time_hints", []) or [])
    today_raw = ""
    for h in time_list:
        if isinstance(h, str) and h.startswith("today="):
            today_raw = h.split("=", 1)[1]
            break
    as_of = str(scope_dict.get("as_of_date") or today_raw or "")
    th = TimeHints(
        as_of_date=as_of,
        inferred_year=as_of[:4] if len(as_of) >= 4 and as_of[:4].isdigit() else "",
        partition_format="yyyy-MM-dd",
    )

    from ..runtime.synthesis.config import load_synthesis_config
    from ..runtime.synthesis.subgraph_context import subgraph_to_llm_context
    from ..runtime.synthesis.path_hint import synthesize_path_hint

    _syn_cfg = load_synthesis_config()
    _mechanical_hint = _build_path_hint(pack, domain)
    _path_hint = synthesize_path_hint(
        subgraph_text=subgraph_to_llm_context(pack),
        domain=domain,
        query=req.query,
        time_hints=time_list,
        fallback=_mechanical_hint,
        cfg=_syn_cfg["path_hint"],
    )

    relevance = _compute_relevance(pipe["anchors"], req.query, req.relevance_threshold, domain=domain)

    # 当 relevance 判定为 no_match 时，清空无关字段，避免返回误导内容
    if relevance.status == "no_match":
        return SearchContextResponse(
            session_ref=session_ref,
            path_hint="",
            schema_prompt="",
            primary_metrics=[],
            alternative_metrics=[],
            knowledge_notes=[],
            similar_experiences=[],
            ambiguous=False,
            ambiguity_candidates=[],
            time_hints=th,
            relevance=relevance,
            operation=pack.operation if req.include_operation else None,
            debug=pack.debug if req.include_debug else None,
        )

    return SearchContextResponse(
        session_ref=session_ref,
        path_hint=_path_hint,
        schema_prompt=schema_prompt,
        primary_metrics=primary_metrics,
        alternative_metrics=alternative_metrics,
        knowledge_notes=knowledge_notes,
        similar_experiences=_similar_experiences_from_cards(pack.experience.cards),
        ambiguous=ambiguous,
        ambiguity_candidates=amb_candidates,
        time_hints=th,
        relevance=relevance,
        operation=pack.operation if req.include_operation else None,
        debug=pack.debug if req.include_debug else None,
    )


@router.post("/search_context")
async def cm_search_context(req: SearchContextRequest, request: Request):
    """Search context — SSE progress when ``stream=true`` (default), else sync JSON."""
    from .ctx_pipeline import _run_pipeline_front, _run_pipeline_front_sse, _sse_event

    driver = _driver(request)
    store = _store(request)
    scope_dict: dict[str, Any] = {}
    if req.scope:
        scope_dict = req.scope.model_dump(exclude_none=True)

    session, session_ref = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "context.session.ensure",
        _ensure_session,
        request,
        req.session_ref,
        scope=scope_dict,
        original_query=req.query,
        datasource_id=req.datasource_id,
    )
    db_id = await request.app.state.blocking_io.run(
        BlockingPool.GRAPH,
        "context.database.resolve",
        _resolve_db_id,
        db_id=None,
        session=session,
        driver=driver,
    )
    if scope_dict.get("domain"):
        anchor_domain_on_session(session, str(scope_dict["domain"]))

    # Extract domain for pipeline: explicit scope > auto-detect from query text
    domain = str(scope_dict.get("domain") or "")
    if not domain:
        from .retrieval import detect_domain
        domain = await request.app.state.blocking_io.run(
            BlockingPool.GRAPH,
            "context.detect_domain",
            detect_domain,
            driver,
            req.query,
        ) or ""

    if not req.stream:
        try:
            pipe = await request.app.state.blocking_io.run(
                BlockingPool.GRAPH,
                "context.search_pipeline",
                _run_pipeline_front,
                driver=driver,
                query=req.query,
                db_id=db_id,
                domain=domain,
            )
        except BlockingIOError:
            raise
        except Exception as exc:
            log.exception("search_context pipeline failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc
        return await request.app.state.blocking_io.run(
            BlockingPool.NETWORK,
            "context.response.assemble",
            _finish_search_context_from_pipe,
            req=req,
            session=session,
            session_ref=session_ref,
            store=store,
            pipe=pipe,
            scope_dict=scope_dict,
        )

    async def _stream():
        pipe: dict[str, Any] = {}
        try:
            async for sse_frame in _run_pipeline_front_sse(
                driver=driver, query=req.query, db_id=db_id, result_holder=pipe,
                domain=domain,
                governor=request.app.state.blocking_io,
            ):
                yield sse_frame
        except Exception as exc:
            log.exception("search_context pipeline failed: %s", exc)
            yield _sse_event("error", {"detail": f"Pipeline error: {exc}"})
            return

        try:
            resp = await request.app.state.blocking_io.run(
                BlockingPool.NETWORK,
                "context.response.assemble",
                _finish_search_context_from_pipe,
                req=req,
                session=session,
                session_ref=session_ref,
                store=store,
                pipe=pipe,
                scope_dict=scope_dict,
            )
        except Exception as exc:
            log.exception("search_context assemble failed: %s", exc)
            yield _sse_event("error", {"detail": f"Assemble error: {exc}"})
            return
        yield _sse_event("done", resp.model_dump(mode="json"))

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------- #
# L2 — search_event, explore_entity, execute_sql
# ---------------------------------------------------------------------- #

@router.post("/search_event", response_model=SearchEventResponse)
def cm_search_event(req: SearchEventRequest, request: Request) -> SearchEventResponse:
    """L2：自然语言检索知识图 Event 结点（全文 + 向量 RRF，仅 :Event）。
    
    Returns EventCards (not EventSearchHit) with:
    - Capped summary (CFG.pack_event_desc_chars, default 400)
    - relevance_score for ranking
    - Sorted by relevance descending
    """
    from .retrieval import search_events, relevance_gate
    from .semantic_pack import build_event_cards

    q = (req.query or "").strip()
    if not q:
        raise HTTPException(status_code=422, detail="query is required")
    driver = _driver(request)
    try:
        rows = search_events(driver, q, limit=req.limit)
    except Exception as exc:
        log.exception("search_event failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    filtered, gate = relevance_gate(
        rows, q, threshold=req.relevance_threshold,
        name_field="name", score_field="score",
    )

    if gate["status"] == "no_match":
        return SearchEventResponse(
            query=q,
            events=[],
            relevance=QueryRelevance(
                status="no_match",
                score=gate["score"],
                detail="未找到与该问题相关的事件",
            ),
        )

    # Build EventCards with capped summaries and relevance scores
    events = build_event_cards(q, [r for r in filtered if r.get("key")])

    relevance = QueryRelevance(
        status=gate["status"],
        score=gate["score"],
        detail=f"匹配到「{gate['matched_name']}」" if gate["matched_name"] else "",
    )
    return SearchEventResponse(query=q, events=events, relevance=relevance)


def _explore_entity_impl(
    request: Request,
    session: ContextSession,
    session_ref: str,
    entity_name: str,
    relevance_threshold: Optional[float] = None,
    domain: Optional[str] = None,
) -> Union[ExploreEntityHit, ExploreEntityAmbiguous]:
    from .ctx_service import zoom_entity, ZoomEntityRequest

    driver = _driver(request)
    dom = (domain or "").strip()
    if not dom:
        dom = (session.scope or {}).get("domain") or ""
    if not dom and session.snapshots:
        for a in getattr(session.current.anchors, "anchors", []) or []:
            d = getattr(a, "domain", "") or ""
            if d:
                dom = d
                session.scope["domain"] = d
                break
    if not dom:
        try:
            dom, _ = _domain_from_request(
                request, domain=None, session_ref=session.session_ref
            )
        except HTTPException:
            dom = ""
    if not dom:
        from .retrieval import detect_domain, list_domains

        dom = detect_domain(driver, entity_name) or ""
        if not dom:
            for dname in list_domains(driver)[:12]:
                trial = resolve_entity(driver, domain=dname, name=entity_name, kind="metric")
                if trial.kind in ("hit", "ambiguous"):
                    dom = dname
                    break
        if dom:
            session.scope["domain"] = dom
            _store(request).put(session)
    if dom:
        resolved = resolve_entity(driver, domain=dom, name=entity_name, kind="metric")
        if resolved.kind == "not_found":
            resolved = resolve_entity_any(driver, domain=dom, name=entity_name)
    else:
        raise HTTPException(
            status_code=400,
            detail="Cannot infer domain for explore_entity. Pass domain in the request, use search_context first, or set it via session scope.",
        )

    if resolved.kind == "ambiguous":
        return ExploreEntityAmbiguous(
            session_ref=session_ref,
            ambiguity_candidates=to_ambiguity_candidates(resolved.candidates),
            hint="建议追问或使用更精确的名称重新查询",
        )
    if resolved.kind == "not_found":
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_name}")

    # Apply relevance threshold (default 0.40 when omitted — same as search_context / search_event).
    from .retrieval import relevance_gate

    hit = resolved.candidates[0] if resolved.candidates else None
    # match_confidence is already RRF-scaled to 0–1; relevance_gate expects raw RRF (~0–0.05).
    raw_rrf = (hit.match_confidence if hit else 0.0) / 20.0
    mock_row = {
        "name": (hit.name if hit else resolved.canonical_name) or entity_name,
        "aliases": [],
        "score": raw_rrf,
    }
    _, gate = relevance_gate([mock_row], entity_name, threshold=relevance_threshold)
    relevance = QueryRelevance(
        status=gate["status"],
        score=gate["score"],
        detail=(
            f"匹配到「{gate['matched_name']}」"
            if gate.get("matched_name")
            else ("匹配度较低，可能没有对应数据" if gate["status"] == "low_confidence" else "未找到与该问题相关的数据")
        ),
    )
    if gate["status"] == "no_match":
        raise HTTPException(
            status_code=404,
            detail=f"Entity not found: {entity_name} (relevance score {gate['score']:.3f} below threshold)",
        )

    sub_req = ZoomEntityRequest(session_ref=session_ref, entity_name=resolved.canonical_name)
    detail = zoom_entity(sub_req, request)

    # Cap lists using CFG.pack_max_* settings
    max_cols = CFG.pack_max_source_columns
    max_dims = CFG.pack_max_drill_dimensions
    max_filters = CFG.pack_max_common_filters
    drill = [
        DrillDimension(name=d.name, relationship="可按此维度过滤或分组")
        for d in (detail.drill_dimensions or [])
    ][:max_dims]
    common_filters = [
        CommonFilter(description=f, sql_fragment=f)
        for f in (detail.common_filters or [])
        if f
    ][:max_filters]
    source_cols = [
        SourceColumn(name=c.name, dataset=c.table, role=c.role or "measure")
        for c in (detail.source_columns or [])
    ][:max_cols]
    from ..runtime.synthesis.config import load_synthesis_config
    from ..runtime.synthesis.subgraph_context import subgraph_to_llm_context
    from ..runtime.synthesis.explore_entity import (
        _placeholder_fields,
        apply_field_toggles,
        expand_2hop,
        pick_2hop_targets_llm,
        synthesize_entity_context_llm,
        synthesize_from_subgraph,
    )

    cfg = load_synthesis_config()["explore_entity"]
    if not cfg.get("enabled"):
        fields = _placeholder_fields(detail)
    else:
        original_query = ""
        if cfg.get("context", {}).get("include_original_query", True):
            original_query = (getattr(session, "original_query", "") or "").strip()

        # Prefer the session multi-hop subgraph as the synthesis substrate.
        subgraph_text = ""
        try:
            from .ctx_assemble import assemble_context_pack
            if session.snapshots:
                pack = assemble_context_pack(
                    session_ref, session.snapshot_index, session.current,
                    include_debug=False, endpoint="explore_entity",
                )
                subgraph_text = subgraph_to_llm_context(
                    pack, center_key=getattr(detail, "key", "") or None
                )
        except Exception as exc:  # noqa: BLE001 - never crash explore_entity
            log.warning("explore_entity subgraph substrate failed: %s", exc)

        try:
            if subgraph_text:
                synth_fields = synthesize_from_subgraph(
                    detail=detail, subgraph_text=subgraph_text,
                    original_query=original_query, cfg=cfg,
                )
            else:
                hop2: dict[str, list[dict[str, Any]]] = {}
                if cfg.get("hop2", {}).get("enabled", True):
                    picked = pick_2hop_targets_llm(detail, original_query=original_query, cfg=cfg)
                    if picked:
                        hop2 = expand_2hop(
                            driver, picked,
                            max_neighbors_per_node=int(cfg.get("context", {}).get("max_neighbors_per_node", 3)),
                        )
                synth_fields = synthesize_entity_context_llm(
                    entity_type=(detail.label or "Metric"),
                    detail=detail, hop2=hop2, original_query=original_query, cfg=cfg,
                )
            fields = apply_field_toggles(synth_fields, detail, cfg.get("fields", {}))
        except Exception as exc:  # noqa: BLE001 - never crash explore_entity
            log.warning("explore_entity synthesis stage failed: %s", exc)
            fields = _placeholder_fields(detail)

    # Build related_events and knowledge_notes using semantic_pack builders
    from .semantic_pack import build_event_cards, build_knowledge_notes
    
    # Convert detail.related_events to EventCard format
    related_events_rows = [
        {
            "name": ev.name,
            "type": getattr(ev, "type", ""),
            "scope": getattr(ev, "scope", ""),
            "description": ev.description,
            "date_from": str(getattr(ev, "date_from", "")),
            "date_to": str(getattr(ev, "date_to", "")),
            "about_entity_name": getattr(ev, "about_entity_name", ""),
            "vec_score": 0.0,  # EntityDetail doesn't have vec_score
        }
        for ev in (detail.related_events or [])
    ]
    related_events = build_event_cards(
        entity_name,
        related_events_rows,
        max_items=CFG.pack_max_events,
    )
    
    # Convert detail.related_knowledge to KnowledgeNote format
    knowledge_anchors = [
        {
            "key": kn.key,
            "name": kn.name,
            "description": kn.summary,
            "label": kn.label,
            "vec_score": 0.0,
        }
        for kn in (detail.related_knowledge or [])
    ]
    knowledge_notes = build_knowledge_notes(
        entity_name,
        knowledge_anchors,
        max_items=CFG.pack_max_knowledge,
    )
    
    hit_kwargs: dict[str, Any] = dict(
        session_ref=session_ref,
        entity_type=detail.label or "Metric",
        name=detail.name,
        domain=dom,
        match_confidence=detail.match_confidence,
        summary=fields["summary"],
        usage_guidance=fields["usage_guidance"],
        definition=detail.definition,
        source_columns=source_cols,
        drill_dimensions=drill,
        common_filters=common_filters,
        related_metrics_nl=fields["related_metrics_nl"][:CFG.pack_max_related_metrics],
        experience_hints=fields["experience_hints"][:CFG.pack_max_experience_hints],
        related_events=related_events,
        knowledge_notes=knowledge_notes,
        relevance=relevance,
    )
    return ExploreEntityHit(**hit_kwargs)


@router.post("/explore_entity")
async def cm_explore_entity(req: ExploreEntityRequest, request: Request):
    session, session_ref = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "context.session.ensure",
        _ensure_session,
        request,
        req.session_ref,
        original_query=req.entity_name,
        datasource_id=req.datasource_id,
    )
    result = await request.app.state.blocking_io.run(
        BlockingPool.GRAPH,
        "context.entity.explore",
        _explore_entity_impl,
        request,
        session,
        session_ref,
        req.entity_name,
        req.relevance_threshold,
        domain=req.domain,
    )
    if isinstance(result, ExploreEntityAmbiguous):
        return JSONResponse(status_code=200, content=result.model_dump())
    return result


@router.get("/downloads/{filename}")
def download_sql_result(filename: str, request: Request):
    """Download previously saved SQL result CSV."""
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files available")

    downloads_dir = _sql_downloads_dir()
    file_path = downloads_dir / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Download file not found or expired")

    _cleanup_expired_downloads()

    return FileResponse(
        path=str(file_path),
        media_type="text/csv",
        filename=filename,
    )


@router.post("/execute_sql", response_model=ExecuteSqlResponse)
async def cm_execute_sql(req: ExecuteSqlRequest, request: Request) -> ExecuteSqlResponse:
    from datapaw.context.resource_budget import current_request_budget

    session, session_ref = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "context.session.ensure",
        _ensure_session,
        request,
        req.session_ref,
        datasource_id=req.datasource_id,
    )
    driver = _driver(request)
    # datasource_id: 请求 > session > 按 SQL 引用的表 datasource_id 推断（warehouse→ODPS，其余→OLTP）
    ds_id = await request.app.state.blocking_io.run(
        BlockingPool.GRAPH,
        "context.datasource.resolve",
        _effective_datasource_id,
        req.datasource_id,
        session,
        sql=req.sql,
        driver=driver,
        domain=(session.scope or {}).get("domain") or "",
    )

    max_rows = current_request_budget().cap_sql_rows(req.max_rows)

    # Check cache through the bounded file pool (exact SQL + effective budget).
    cached = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "sql.cache.get",
        _sql_cache.get,
        req.sql,
        max_rows,
    )
    download_url: Optional[str] = None
    expires_in: Optional[int] = None

    if cached:
        log.debug("SQL cache hit: %s...", req.sql[:60])
        cached.touch()
        raw = cached.result
        exec_status = cached.exec_status
        download_url = cached.download_url
        if download_url:
            expires_in = cached.expires_in_seconds()
    else:
        # Cache miss - execute SQL off the event loop (ODPS/JDBC are sync-blocking).
        raw = await execute_sql_async(
            req.sql, max_rows=max_rows,
            datasource_id=ds_id,
            governor=request.app.state.blocking_io,
        )
        exec_status = "success"
        if raw.error:
            exec_status = "error"
        elif raw.row_count == 0:
            exec_status = "empty"
        elif (raw.elapsed_ms or 0) >= req.slow_ms_threshold and raw.row_count > 0:
            exec_status = "slow"

        if raw.rows:
            try:
                download_id, file_path = await request.app.state.blocking_io.run(
                    BlockingPool.FILE,
                    "sql.save_csv",
                    _save_sql_results_to_csv,
                    columns=raw.columns,
                    rows=raw.rows,
                )
                download_url = f"{_CM_LOCAL_BASE_URL}/api/v1/cm/downloads/{download_id}.csv"
                expires_in = _SQL_DOWNLOAD_TTL_SECONDS

                _sql_cache.put(
                    req.sql,
                    max_rows,
                    raw,
                    exec_status,
                    download_url,
                    file_path,
                    ttl=_SQL_DOWNLOAD_TTL_SECONDS,
                )
            except ResourceBudgetExceeded:
                raise
            except Exception as e:
                log.warning("Failed to save SQL result for download: %s", e)

    preview_rows = raw.rows[:_SQL_PREVIEW_ROWS]

    return ExecuteSqlResponse(
        session_ref=session_ref,
        exec_status=exec_status,  # type: ignore[arg-type]
        sql=raw.sql,
        columns=list(raw.columns),
        rows=preview_rows,
        preview_row_count=len(preview_rows),
        truncated=raw.truncated,
        elapsed_ms=raw.elapsed_ms,
        error=raw.error,
        download_url=download_url,
        total_row_count=raw.row_count,
        expires_in_seconds=expires_in,
    )


_SQL_STREAM_HEARTBEAT_SECONDS = float(
    os.environ.get("CM_SQL_STREAM_HEARTBEAT_SECONDS", "5")
)


@router.post("/execute_sql_stream")
async def cm_execute_sql_stream(req: ExecuteSqlRequest, request: Request):
    """SSE variant of execute_sql with periodic heartbeat events.

    Events emitted:
    - ``heartbeat`` — periodic keep-alive while SQL is running
    - ``done`` — final result (same shape as sync ExecuteSqlResponse)
    - ``error`` — execution failure
    """
    from datapaw.context.resource_budget import current_request_budget

    from .ctx_pipeline import _sse_event

    session, session_ref = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "context.session.ensure",
        _ensure_session,
        request,
        req.session_ref,
        datasource_id=req.datasource_id,
    )
    driver = _driver(request)
    ds_id = await request.app.state.blocking_io.run(
        BlockingPool.GRAPH,
        "context.datasource.resolve",
        _effective_datasource_id,
        req.datasource_id,
        session,
        sql=req.sql,
        driver=driver, domain=(session.scope or {}).get("domain") or "",
    )
    max_rows = current_request_budget().cap_sql_rows(req.max_rows)

    async def _stream():
        try:
            cached = await request.app.state.blocking_io.run(
                BlockingPool.FILE,
                "sql.cache.get",
                _sql_cache.get,
                req.sql,
                max_rows,
            )
        except BlockingIOError as exc:
            yield _sse_event("error", {
                "code": type(exc).__name__,
                "pool": exc.pool.value,
                "operation": exc.operation,
                "detail": str(exc),
            })
            return
        download_url: Optional[str] = None
        expires_in: Optional[int] = None

        if cached:
            cached.touch()
            raw = cached.result
            exec_status = cached.exec_status
            download_url = cached.download_url
            if download_url:
                expires_in = cached.expires_in_seconds()
        else:
            future = asyncio.create_task(
                request.app.state.blocking_io.run(
                    BlockingPool.SQL,
                    "sql.execute_stream",
                    topology_execute_sql,
                    req.sql,
                    max_rows=max_rows,
                    datasource_id=ds_id,
                ),
                name="cm-sql-stream",
            )

            start = time.monotonic()
            yield _sse_event("heartbeat", {
                "stage": "submitted",
                "elapsed_ms": 0,
            })

            raw = None
            try:
                while not future.done():
                    try:
                        raw = await asyncio.wait_for(
                            asyncio.shield(future),
                            timeout=_SQL_STREAM_HEARTBEAT_SECONDS,
                        )
                        break
                    except asyncio.TimeoutError:
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        yield _sse_event("heartbeat", {
                            "stage": "executing",
                            "elapsed_ms": elapsed_ms,
                        })

                if raw is None:
                    raw = future.result()
            except BlockingIOError as exc:
                yield _sse_event("error", {
                    "code": type(exc).__name__,
                    "pool": exc.pool.value,
                    "operation": exc.operation,
                    "detail": str(exc),
                })
                return

            exec_status = "success"
            if raw.error:
                exec_status = "error"
            elif raw.row_count == 0:
                exec_status = "empty"
            elif (raw.elapsed_ms or 0) >= req.slow_ms_threshold and raw.row_count > 0:
                exec_status = "slow"

            if raw.rows:
                try:
                    download_id, file_path = await request.app.state.blocking_io.run(
                        BlockingPool.FILE,
                        "sql.save_csv",
                        _save_sql_results_to_csv,
                        columns=raw.columns, rows=raw.rows,
                    )
                    download_url = f"{_CM_LOCAL_BASE_URL}/api/v1/cm/downloads/{download_id}.csv"
                    expires_in = _SQL_DOWNLOAD_TTL_SECONDS
                    _sql_cache.put(
                        req.sql, max_rows, raw, exec_status,
                        download_url, file_path, ttl=_SQL_DOWNLOAD_TTL_SECONDS,
                    )
                except ResourceBudgetExceeded:
                    raise
                except Exception as e:
                    log.warning("Failed to save SQL result for download: %s", e)

        preview_rows = raw.rows[:_SQL_PREVIEW_ROWS]
        yield _sse_event("done", ExecuteSqlResponse(
            session_ref=session_ref,
            exec_status=exec_status,
            sql=raw.sql,
            columns=list(raw.columns),
            rows=preview_rows,
            preview_row_count=len(preview_rows),
            truncated=raw.truncated,
            elapsed_ms=raw.elapsed_ms,
            error=raw.error,
            download_url=download_url,
            total_row_count=raw.row_count,
            expires_in_seconds=expires_in,
        ).model_dump(mode="json"))

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/recall_experience", response_model=RecallExperienceResponse)
async def cm_recall_experience(
    req: RecallExperienceRequest, request: Request
) -> RecallExperienceResponse:
    from .ctx_service import recall_experience as ctx_recall, RecallExperienceRequest as CtxRecall

    session, session_ref = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "context.session.ensure",
        _ensure_session,
        request,
        req.session_ref,
        datasource_id=req.datasource_id,
    )
    focus = None
    if req.focus:
        from .ctx_service import ExperienceFocus

        focus = ExperienceFocus(
            task_type=None,
            avoid_only=req.focus.avoid_only,
            with_supersede_chain=False,
        )
    frag = await request.app.state.blocking_io.run(
        BlockingPool.GRAPH,
        "context.experience.recall",
        ctx_recall,
        CtxRecall(session_ref=session_ref, focus=focus),
        request,
    )
    do_hints: list[str] = []
    avoid_hints: list[str] = []
    cards_out: list[Any] = []
    for c in frag.new_cards:
        lesson = (c.lesson or c.strategy_semantics or "")[:200]
        if c.polarity in ("negative", "avoid"):
            avoid_hints.append(lesson)
        else:
            do_hints.append(lesson)
        cards_out.append(
            {
                "polarity": c.polarity,
                "lesson": lesson,
                "confidence": c.composite_score,
            }
        )
    guidance = (
        f"正向：{'；'.join(do_hints[:3])}" if do_hints else ""
    ) + (f" 避免：{'；'.join(avoid_hints[:3])}" if avoid_hints else "")
    from .cm_models import ExperienceCardBrief, RecallExperienceStats  # noqa: PLC0415

    return RecallExperienceResponse(
        session_ref=session_ref,
        guidance_summary=guidance.strip() or "暂无额外经验",
        do_hints=do_hints,
        avoid_hints=avoid_hints,
        cards=[ExperienceCardBrief(**x) for x in cards_out],
        stats=RecallExperienceStats(
            card_count=frag.stats.visible_n,
            top_score=frag.stats.top_score,
        ),
    )
