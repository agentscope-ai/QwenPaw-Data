"""MG (Metadata Graph) read-only API — wraps semantic_store.py.

Prefix: ``/api/v1/mg``

All routes are GET (read-only). MG data is managed by the upstream config
pipeline; the frontend only browses.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..utils import get_logger, graph_session
from . import semantic_store as store
from .cm_resolve import resolve_read_datasource
from .response_envelope import clamp_page, fail, paginated, success

log = get_logger("api.mg_readonly")

router = APIRouter(prefix="/api/v1/admin/mg", tags=["mg-readonly"])


def _driver(request: Request):
    """Extract the Neo4j driver from app state."""
    return request.app.state.driver


def _read_ds_id(domain: str, datasource_id: str | None) -> str:
    """Resolve the effective datasource_id for a read request."""
    return resolve_read_datasource(domain, req_datasource_id=datasource_id or "")


# ═══════════════════════════════════════════════════════════════════════ #
#  Domain
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/domains")
def list_domains(
    request: Request,
    datasource_id: str | None = Query(None),
    page: int = 1,
    page_size: int = 20,
):
    """Paginated listing of all Domains, optionally filtered by datasource."""
    ds = (datasource_id or "").strip()
    domains = store.list_domain_records(_driver(request), datasource_id=ds)
    items = [d.model_dump() for d in domains]
    page, page_size = clamp_page(page, page_size)
    total = len(items)
    start = (page - 1) * page_size
    return paginated(items[start : start + page_size], total=total, page=page, page_size=page_size)


@router.get("/domains/{name}")
def get_domain_detail(
    name: str,
    request: Request,
    datasource_id: str | None = Query(None),
):
    """Return a single Domain with metric/dimension/dataset counts."""
    driver = _driver(request)
    ds = (datasource_id or "").strip()
    domains = store.list_domain_records(driver, datasource_id=ds)
    domain_obj = next((d for d in domains if d.name == name), None)
    if not domain_obj:
        fail("NOT_FOUND", f"Domain not found: {name}", status_code=404)
        return  # unreachable, fail raises
    data = domain_obj.model_dump()
    data["stats"] = _domain_stats(driver, name, ds)
    return success(data)


def _domain_stats(driver, domain: str, datasource_id: str = "") -> dict:
    """Count active metrics, dimensions, and datasets under a Domain."""
    ds = (datasource_id or "").strip()
    cypher = """
    MATCH (d:Domain {name: $domain})
    WHERE ($ds = '' OR d.datasource_id = $ds)
    OPTIONAL MATCH (d)-[:HAS_METRIC]->(m:Metric)
      WHERE m.valid_to IS NULL OR m.valid_to > datetime()
    OPTIONAL MATCH (d)-[:HAS_DIMENSION]->(dim:Dimension)
    OPTIONAL MATCH (d)-[:HAS_DATASET]->(ds2:Dataset)
    RETURN count(DISTINCT m) AS metric_count,
           count(DISTINCT dim) AS dimension_count,
           count(DISTINCT ds2) AS dataset_count
    """
    with graph_session(driver) as s:
        rec = s.run(cypher, domain=domain, ds=ds).single()
    if not rec:
        return {"metric_count": 0, "dimension_count": 0, "dataset_count": 0}
    return {
        "metric_count": int(rec["metric_count"]),
        "dimension_count": int(rec["dimension_count"]),
        "dataset_count": int(rec["dataset_count"]),
    }


# ═══════════════════════════════════════════════════════════════════════ #
#  Metric
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/metrics")
def list_metrics(
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
    q: str | None = Query(None),
    page: int = 1,
    page_size: int = 20,
):
    """Paginated listing of Metrics in a Domain with keyword search."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        metrics = store.list_metrics(driver, domain, datasource_id=ds_id)
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)
        return
    items = [m.model_dump() for m in metrics]
    if q:
        ql = q.lower()
        items = [
            m for m in items
            if ql in (m.get("metric_name") or "").lower()
            or any(ql in a.lower() for a in m.get("aliases", []))
        ]
    page, page_size = clamp_page(page, page_size)
    total = len(items)
    start = (page - 1) * page_size
    return paginated(items[start : start + page_size], total=total, page=page, page_size=page_size)


@router.get("/metrics/{metric_name}")
def get_metric_detail(
    metric_name: str,
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
):
    """Return full detail of a single Metric."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        detail = store.get_metric_detail(driver, domain, metric_name, datasource_id=ds_id)
        return success(detail.model_dump())
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)


# ═══════════════════════════════════════════════════════════════════════ #
#  Dimension
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/dimensions")
def list_dimensions(
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
    page: int = 1,
    page_size: int = 20,
):
    """Paginated listing of Dimension names in a Domain."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        names = store.list_dimension_names(driver, domain, datasource_id=ds_id)
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)
        return
    page, page_size = clamp_page(page, page_size)
    total = len(names)
    start = (page - 1) * page_size
    return paginated(names[start : start + page_size], total=total, page=page, page_size=page_size)


@router.get("/dimensions/{dim_name}")
def get_dimension_detail(
    dim_name: str,
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
):
    """Return a Dimension with hierarchy, values, and bound metrics."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        dim = store.get_dimension(driver, domain, dim_name, datasource_id=ds_id)
        data = dim.model_dump()
        hierarchy = store.get_dimension_hierarchy(driver, domain, dim_name, datasource_id=ds_id)
        data["hierarchy"] = {"parent": hierarchy.parent, "children": hierarchy.children}
        records, total_count = store.get_dimension_value_records(
            driver, domain, dim_name, limit=50, datasource_id=ds_id,
        )
        data["values"] = [r.value for r in records]
        data["values_total"] = total_count
        bound = store.get_metrics_for_dimension(driver, domain, dim_name, datasource_id=ds_id)
        data["bound_metrics"] = [m.metric_name for m in bound]
        return success(data)
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)


# ═══════════════════════════════════════════════════════════════════════ #
#  Dataset
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/datasets")
def list_datasets(
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
    page: int = 1,
    page_size: int = 20,
):
    """Paginated listing of Datasets in a Domain."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        datasets = store.list_datasets(driver, domain, datasource_id=ds_id)
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)
        return
    items = [d.model_dump() for d in datasets]
    page, page_size = clamp_page(page, page_size)
    total = len(items)
    start = (page - 1) * page_size
    return paginated(items[start : start + page_size], total=total, page=page, page_size=page_size)


@router.get("/datasets/{dataset_name}")
def get_dataset_schema(
    dataset_name: str,
    request: Request,
    domain: str = Query(...),
    datasource_id: str | None = Query(None),
):
    """Return a Dataset's column schema."""
    driver = _driver(request)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        schema = store.get_dataset_schema(driver, domain, dataset_name, datasource_id=ds_id)
        return success(schema.model_dump())
    except KeyError as exc:
        fail("NOT_FOUND", str(exc.args[0] if exc.args else exc), status_code=404)


# ═══════════════════════════════════════════════════════════════════════ #
#  MG Node edges (Phase 2 — stub returns empty for now)
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/nodes/{key:path}/edges")
def list_node_edges(
    key: str,
    request: Request,
    direction: str = "both",
    rel_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    """Paginated edge listing for any MG node."""
    from .retrieval import list_node_edges as _list_node_edges

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    try:
        edges, total = _list_node_edges(
            driver, key,
            direction=direction,
            rel_type=rel_type,
            page=page,
            page_size=page_size,
        )
        return paginated({"edges": edges}, total=total, page=page, page_size=page_size)
    except Exception as exc:
        log.exception("list_node_edges: %s", exc)
        fail("INTERNAL_ERROR", str(exc), status_code=500)
