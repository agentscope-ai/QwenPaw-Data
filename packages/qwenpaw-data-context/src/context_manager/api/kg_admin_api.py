"""KG (Knowledge Graph) CRUD API — RESTful router wrapping kg_admin.py.

Prefix: ``/api/v1/admin/kg``
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from . import kg_admin
from .kg_admin_models import (
    AboutRequest,
    AdjacentEdgeDeleteRequest,
    BatchDeleteRequest,
    CrossGraphEdgeDeleteRequest,
    CrossGraphEdgeRequest,
    EdgeDeleteByTypeRequest,
    EdgePropertiesUpdateRequest,
    EntityUpsertRequest,
    EventUpsertRequest,
    GlobalEdgePurgeRequest,
    RelatedToDeleteRequest,
    RelatedToRequest,
)
from .response_envelope import clamp_page, fail, paginated, success

router = APIRouter(prefix="/api/v1/admin/kg", tags=["kg-admin"])


def _driver(request: Request):
    """Extract the Neo4j driver from app state."""
    return request.app.state.driver


def _success_after_write(result):
    """Wrap a successful graph mutation, dropping stale global snapshots."""
    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return success(result)


# ═══════════════════════════════════════════════════════════════════════ #
#  Entity
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/entities")
def list_entities(
    request: Request,
    q: str = "",
    type: str | None = None,
    lifecycle_state: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated Entity listing with type and lifecycle filters."""
    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows = kg_admin.list_knowledge_nodes(driver, q=q, kind="entity", limit=500)
    if type:
        rows = [r for r in rows if r.get("type") == type]
    if lifecycle_state:
        rows = [r for r in rows if r.get("lifecycle_state") == lifecycle_state]
    total = len(rows)
    start = (page - 1) * page_size
    return paginated(rows[start : start + page_size], total=total, page=page, page_size=page_size)


@router.post("/entities/batch-delete")
def batch_delete_entities(body: BatchDeleteRequest, request: Request):
    """Delete multiple Entity/Event nodes by key."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_knowledge_nodes_batch(driver, body.keys)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.get("/entities/{key:path}")
def get_entity(
    key: str,
    request: Request,
    include_neighbors: bool = True,
    neighbor_limit: int = 50,
):
    """Return an Entity with optional neighbor nodes."""
    driver = _driver(request)
    node = kg_admin.get_knowledge_node(driver, key)
    if not node:
        fail("NOT_FOUND", f"Entity not found: {key}", status_code=404)
    data: dict = {"entity": node}
    if include_neighbors:
        data["neighbors"] = kg_admin.list_neighbors(driver, key, limit=neighbor_limit)
    return success(data)


@router.put("/entities/{key:path}")
def upsert_entity(key: str, body: EntityUpsertRequest, request: Request):
    """Create or update an Entity node."""
    driver = _driver(request)
    if not key.startswith("ent:"):
        fail("INVALID_KEY_FORMAT", "Entity key 须以 ent: 开头")
    try:
        result = kg_admin.upsert_entity(
            driver,
            key=key,
            canonical_name=body.canonical_name,
            type_=body.type,
            aliases=body.aliases,
            description=body.description,
            lifecycle_state=body.lifecycle_state,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/entities/{key:path}")
def delete_entity(key: str, request: Request):
    """Delete a single Entity node and its edges."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_knowledge_node(driver, key)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


# ═══════════════════════════════════════════════════════════════════════ #
#  Event
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/events")
def list_events(
    request: Request,
    q: str = "",
    type: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated Event listing with optional type filter."""
    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows = kg_admin.list_knowledge_nodes(driver, q=q, kind="event", limit=500)
    if type:
        rows = [r for r in rows if r.get("type") == type]
    total = len(rows)
    start = (page - 1) * page_size
    return paginated(rows[start : start + page_size], total=total, page=page, page_size=page_size)


@router.get("/events/{key:path}")
def get_event(
    key: str,
    request: Request,
    include_neighbors: bool = True,
    neighbor_limit: int = 50,
):
    """Return an Event with optional neighbor nodes."""
    driver = _driver(request)
    node = kg_admin.get_knowledge_node(driver, key)
    if not node:
        fail("NOT_FOUND", f"Event not found: {key}", status_code=404)
    data: dict = {"entity": node}
    if include_neighbors:
        data["neighbors"] = kg_admin.list_neighbors(driver, key, limit=neighbor_limit)
    return success(data)


@router.put("/events/{key:path}")
def upsert_event(key: str, body: EventUpsertRequest, request: Request):
    """Create or update an Event node."""
    driver = _driver(request)
    if not key.startswith("ev:"):
        fail("INVALID_KEY_FORMAT", "Event key 须以 ev: 开头")
    try:
        result = kg_admin.upsert_event(
            driver,
            key=key,
            name=body.name,
            type_=body.type,
            description=body.description,
            date_from=body.date_from,
            date_to=body.date_to,
            scope=body.scope,
            zone=body.zone,
            source_id=body.source_id,
            source_trust=body.source_trust,
            extractor=body.extractor,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/events/{key:path}")
def delete_event(key: str, request: Request):
    """Delete a single Event node and its edges."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_knowledge_node(driver, key)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


# ═══════════════════════════════════════════════════════════════════════ #
#  Edges
# ═══════════════════════════════════════════════════════════════════════ #

@router.post("/edges/related-to")
def create_related_to(body: RelatedToRequest, request: Request):
    """Create or update a RELATED_TO edge between two KG nodes."""
    driver = _driver(request)
    try:
        result = kg_admin.merge_related_to(
            driver,
            from_key=body.from_key,
            to_key=body.to_key,
            relation_subtype=body.relation_subtype,
            description=body.description,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/edges/related-to")
def delete_related_to(body: RelatedToDeleteRequest, request: Request):
    """Remove a RELATED_TO edge between two KG nodes."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_related_to(driver, from_key=body.from_key, to_key=body.to_key)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/edges/about")
def toggle_about(body: AboutRequest, request: Request):
    """Connect or disconnect an Event from an Entity via ABOUT edge."""
    driver = _driver(request)
    try:
        result = kg_admin.set_event_about(
            driver,
            event_key=body.event_key,
            entity_key=body.entity_key,
            connect=body.connect,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/edges/cross-graph")
def create_cross_graph_edge(body: CrossGraphEdgeRequest, request: Request):
    """Create or update a whitelisted cross-graph edge."""
    driver = _driver(request)
    try:
        result = kg_admin.merge_cross_graph_edge(
            driver,
            from_key=body.from_key,
            to_key=body.to_key,
            rel_type=body.rel_type,
            properties=body.properties,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/edges/cross-graph")
def delete_cross_graph_edge(body: CrossGraphEdgeDeleteRequest, request: Request):
    """Remove a whitelisted cross-graph edge."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_cross_graph_edge(
            driver,
            from_key=body.from_key,
            to_key=body.to_key,
            rel_type=body.rel_type,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.patch("/edges/properties")
def update_edge_properties(body: EdgePropertiesUpdateRequest, request: Request):
    """Partially update whitelisted properties on an edge."""
    driver = _driver(request)
    try:
        result = kg_admin.update_edge_properties(
            driver,
            from_key=body.from_key,
            to_key=body.to_key,
            rel_type=body.rel_type,
            properties=body.properties,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/edges/adjacent")
def delete_adjacent_edge(body: AdjacentEdgeDeleteRequest, request: Request):
    """Delete a single directed edge between two nodes."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_adjacent_edge(
            driver,
            anchor_key=body.anchor_key,
            other_key=body.other_key,
            rel_type=body.rel_type,
            direction=body.direction,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/edges/by-type")
def delete_edges_by_type(body: EdgeDeleteByTypeRequest, request: Request):
    """Bulk-delete all edges of a given type from a node."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_edges_from_anchor_by_type(
            driver,
            anchor_key=body.anchor_key,
            rel_type=body.rel_type,
            direction_scope=body.direction_scope,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.get("/edges/rel-types")
def get_rel_types(request: Request):
    """List all relationship types present in the graph."""
    driver = _driver(request)
    types = kg_admin.list_global_edge_purge_types(driver)
    return success({"rel_types": types})


@router.post("/edges/purge-type-global")
def purge_edges_by_type_global(body: GlobalEdgePurgeRequest, request: Request):
    """Delete all edges of a given type that touch any Entity/Event node."""
    driver = _driver(request)
    try:
        result = kg_admin.delete_all_edges_touching_knowledge_nodes_by_type(
            driver, rel_type=body.rel_type,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))
