"""Unified graph explorer API used by the CM Graph frontend."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from qwenpaw_data.context.errors import QwenPawDataError
from qwenpaw_data.context.resource_budget import current_request_budget

from ..utils import get_logger, graph_session
from .kg_admin import _to_jsonable
from .response_envelope import fail, success
from .retrieval import (
    _KNOWLEDGE_LAYER_LABELS,
    _METADATA_LAYER_LABELS,
    _TRACE_LAYER_LABELS,
)

log = get_logger("api.explorer")

router = APIRouter(prefix="/api/v1/admin/explorer", tags=["explorer"])

_NODE_EDITABLE_FIELDS: dict[str, list[str]] = {
    "Claim": ["text", "confidence", "subject_type", "predicate", "object"],
    "Strategy": ["strategy_semantics", "memory_tier", "source_trust", "polarity", "example_query"],
    "StrategyCard": ["strategy_semantics", "memory_tier", "source_trust", "polarity", "example_query"],
    "Entity": ["canonical_name", "type", "aliases", "description", "lifecycle_state"],
    "Event": ["name", "type", "description", "date_from", "date_to", "scope", "source_id", "source_trust", "extractor"],
}

_EDGE_EDITABLE_FIELDS: dict[str, list[str]] = {
    "RELATED_TO": ["relation_subtype", "description", "scope"],
    "ABOUT": ["notes"],
    "SURFACE_METRIC": ["role", "notes"],
    "SURFACE_DIMENSION": ["role"],
}

_LABEL_ZONE: dict[str, str] = {}
for _label in _METADATA_LAYER_LABELS:
    _LABEL_ZONE[_label] = "metadata"
for _label in _TRACE_LAYER_LABELS:
    _LABEL_ZONE[_label] = "trace"
for _label in _KNOWLEDGE_LAYER_LABELS:
    _LABEL_ZONE[_label] = "knowledge"

_STRIP_KEYS = frozenset(
    {
        "embedding",
        "embedding_hash",
        "signature_emb",
        "query_emb",
        "query_embedding",
        "strategy_vec",
    }
)


def _driver(request: Request):
    return request.app.state.driver


def _budgeted_success(data):
    current_request_budget().ensure_response_payload(data)
    return success(data)


class GlobalGraphRequest(BaseModel):
    max_edges: int = Field(200, ge=1, le=20_000)
    max_nodes: int = Field(120, ge=1, le=50_000)
    domain_roots_only: bool = True
    skeleton: bool = True
    zone_mode: Literal["all", "metadata", "trace", "knowledge"] = "all"
    task_roots: bool = False
    max_task_roots: int = Field(10, ge=0, le=200)
    datasource_id: str = ""

    @property
    def resolved_datasource_id(self) -> str:
        return (self.datasource_id or "").strip()


class DomainGraphRequest(BaseModel):
    domain: str
    datasource_id: str = ""


class ExpandNodeRequest(BaseModel):
    key: str
    limit: int = Field(50, ge=1, le=2000)
    zone: str | None = None
    label: str | None = None


class ExpandLayerRequest(BaseModel):
    key: str
    direction: Literal["in", "out", "up", "down"] = "out"
    limit: int = Field(50, ge=1, le=2000)


class SearchNodesRequest(BaseModel):
    query: str = Field("", max_length=500)
    limit: int = Field(50, ge=1, le=100)


class SearchSubgraphRequest(BaseModel):
    query: str
    scope: list[str] = Field(default_factory=lambda: ["metadata", "trace", "knowledge"])
    match_mode: Literal["exact", "fuzzy"] = "fuzzy"
    hops: int = Field(1, ge=1, le=3)
    limit: int = Field(50, ge=1, le=200)


class EdgeDetailRequest(BaseModel):
    source_key: str
    target_key: str
    rel_type: str


@router.post("/global-graph")
def global_graph(body: GlobalGraphRequest, request: Request):
    """Return the initial graph snapshot for the explorer canvas."""
    from .retrieval import global_graph_snapshot

    try:
        max_nodes, max_edges = current_request_budget().cap_graph(
            nodes=body.max_nodes,
            edges=body.max_edges,
        )
        data = global_graph_snapshot(
            _driver(request),
            max_edges=max_edges,
            max_nodes=max_nodes,
            skeleton=body.skeleton,
            domain_roots_only=body.domain_roots_only,
            task_roots=body.task_roots,
            max_task_roots=body.max_task_roots,
            zone_mode=body.zone_mode,
        )
        ds_id = body.resolved_datasource_id
        if ds_id:
            from .datasource_filter import filter_graph_by_datasource
            filtered = filter_graph_by_datasource(data, ds_id)
            if filtered is not None:
                data = filtered
            else:
                data = {"nodes": [], "edges": []}
        return _budgeted_success(data)
    except QwenPawDataError:
        raise
    except Exception as exc:
        log.exception("global_graph failed")
        fail("INTERNAL_ERROR", str(exc), status_code=500)


@router.post("/domain-graph")
def domain_graph(body: DomainGraphRequest, request: Request):
    """Return the metric skeleton for one domain."""
    from .retrieval import domain_graph_snapshot

    try:
        return _budgeted_success(
            domain_graph_snapshot(_driver(request), body.domain.strip())
        )
    except ValueError as exc:
        fail("NOT_FOUND", str(exc), status_code=404)
    except QwenPawDataError:
        raise
    except Exception as exc:
        log.exception("domain_graph failed")
        fail("INTERNAL_ERROR", str(exc), status_code=500)


@router.post("/expand-node")
def expand_node(body: ExpandNodeRequest, request: Request):
    """Expand a node's neighborhood with optional zone/label filtering."""
    from .retrieval import expand_node_snapshot

    try:
        max_edges = current_request_budget().cap_graph(
            nodes=1,
            edges=body.limit,
        )[1]
        data = expand_node_snapshot(
            _driver(request), body.key.strip(), max_edges=max_edges
        )
        if body.zone or body.label:
            data = _filter_expand_result(data, zone=body.zone, label=body.label)
        return _budgeted_success(data)
    except ValueError as exc:
        fail("NOT_FOUND", str(exc), status_code=404)
    except QwenPawDataError:
        raise
    except Exception as exc:
        log.exception("expand_node failed")
        fail("INTERNAL_ERROR", str(exc), status_code=500)


def _filter_expand_result(
    data: dict, *, zone: str | None, label: str | None
) -> dict:
    nodes = data.get("nodes", [])
    center_id = data.get("center")
    filtered_ids: set[str] = set()
    filtered_nodes: list[dict] = []
    for node in nodes:
        node_id = node.get("id", "")
        if node_id == center_id:
            filtered_nodes.append(node)
            filtered_ids.add(node_id)
            continue
        node_label = node.get("group", node.get("label", ""))
        if zone and _LABEL_ZONE.get(node_label, "") != zone:
            continue
        if label and node_label != label:
            continue
        filtered_nodes.append(node)
        filtered_ids.add(node_id)

    filtered_edges = [
        edge
        for edge in data.get("edges", [])
        if edge.get("from", edge.get("source")) in filtered_ids
        and edge.get("to", edge.get("target")) in filtered_ids
    ]
    return {**data, "nodes": filtered_nodes, "edges": filtered_edges}


@router.post("/expand-layer")
def expand_layer(body: ExpandLayerRequest, request: Request):
    """Expand one graph layer, accepting both UI and retrieval direction names."""
    from .retrieval import expand_node_layer

    direction = {"in": "up", "out": "down"}.get(body.direction, body.direction)
    try:
        max_edges = current_request_budget().cap_graph(
            nodes=1,
            edges=body.limit,
        )[1]
        return _budgeted_success(
            expand_node_layer(
                _driver(request),
                body.key.strip(),
                direction=direction,
                max_edges=max_edges,
            )
        )
    except ValueError as exc:
        fail("NOT_FOUND", str(exc), status_code=404)
    except QwenPawDataError:
        raise
    except Exception as exc:
        log.exception("expand_layer failed")
        fail("INTERNAL_ERROR", str(exc), status_code=500)


@router.post("/search-nodes")
def search_nodes(body: SearchNodesRequest, request: Request):
    """Keyword search across graph nodes."""
    from .retrieval import search_explorer_nodes

    query = body.query.strip()
    if not query:
        return success([])
    try:
        return _budgeted_success(
            search_explorer_nodes(_driver(request), query, limit=body.limit)
        )
    except QwenPawDataError:
        raise
    except Exception as exc:
        log.exception("search_nodes failed")
        fail("INTERNAL_ERROR", str(exc), status_code=500)


@router.get("/schema")
def schema_summary(request: Request):
    """Enumerate node labels and relationship types with counts."""
    driver = _driver(request)
    with graph_session(driver) as session:
        label_rows = session.run(
            "MATCH (node) "
            "UNWIND labels(node) AS label "
            "RETURN label, count(*) AS count ORDER BY label"
        ).data()
        rel_rows = session.run(
            "MATCH (source)-[rel]->(target) "
            "RETURN type(rel) AS relationshipType, count(rel) AS count, "
            "head(collect(head(labels(source)))) AS source_label, "
            "head(collect(head(labels(target)))) AS target_label "
            "ORDER BY relationshipType"
        ).data()

    node_labels: list[dict] = []
    for row in label_rows:
        label = str(row.get("label", ""))
        if not label:
            continue
        node_labels.append(
            {
                "label": label,
                "count": int(row.get("count") or 0),
                "zone": _LABEL_ZONE.get(label, "_shared"),
            }
        )

    relationship_types: list[dict] = []
    for row in rel_rows:
        rel_type = str(row.get("relationshipType", ""))
        if not rel_type:
            continue
        count = int(row.get("count") or 0)
        source_label = row.get("source_label") or ""
        target_label = row.get("target_label") or ""
        source_zone = _LABEL_ZONE.get(source_label, "_shared")
        target_zone = _LABEL_ZONE.get(target_label, "_shared")
        relationship_types.append(
            {
                "type": rel_type,
                "count": count,
                "zone": source_zone if source_zone == target_zone else "cross",
                "source_zone": source_zone,
                "target_zone": target_zone,
            }
        )

    return success(
        {"node_labels": node_labels, "relationship_types": relationship_types}
    )


# Keep this more specific route before ``/nodes/{key:path}`` so Starlette does
# not consume the ``/cross-graph`` suffix as part of the key.
@router.get("/nodes/{key:path}/cross-graph")
def cross_graph_neighbors(key: str, request: Request, limit: int = 50):
    """Return neighbors that belong to another graph zone."""
    driver = _driver(request)
    query = """
    MATCH (n {key: $key})-[rel]-(other)
    WHERE n <> other
    RETURN type(rel) AS rel_type,
           CASE WHEN startNode(rel) = n THEN 'out' ELSE 'in' END AS direction,
           other.key AS other_key,
           head(labels(other)) AS other_label,
           coalesce(other.name, other.canonical_name, other.goal, '') AS other_name,
           coalesce(other.zone, '') AS other_zone
    LIMIT $limit
    """
    max_edges = current_request_budget().cap_graph(nodes=1, edges=limit)[1]
    with graph_session(driver) as session:
        rows = session.run(query, key=key, limit=max(1, min(max_edges, 500))).data()
        zone_record = session.run(
            "MATCH (n {key: $key}) "
            "RETURN coalesce(n.zone, head(labels(n))) AS zone LIMIT 1",
            key=key,
        ).single()

    node_zone = ""
    if zone_record:
        raw_zone = str(zone_record["zone"] or "")
        node_zone = _LABEL_ZONE.get(raw_zone, raw_zone if raw_zone in _LABEL_ZONE.values() else "")

    neighbors = []
    for row in rows:
        other_zone = row.get("other_zone") or _LABEL_ZONE.get(row.get("other_label", ""), "")
        if other_zone and other_zone != node_zone:
            neighbors.append(
                {
                    "rel_type": row["rel_type"],
                    "direction": row["direction"],
                    "other_key": row["other_key"],
                    "other_label": row.get("other_label", ""),
                    "other_name": row.get("other_name", ""),
                    "other_zone": other_zone,
                }
            )
    return success(neighbors)


@router.get("/nodes/{key:path}")
def node_detail(key: str, request: Request):
    """Return full properties and editable-field hints for a graph node."""
    with graph_session(_driver(request)) as session:
        record = session.run(
            "MATCH (n {key: $key}) "
            "RETURN labels(n) AS labels, properties(n) AS props LIMIT 1",
            key=key,
        ).single()
    if not record:
        fail("NOT_FOUND", f"Node not found: {key}", status_code=404)

    labels = list(record["labels"] or [])
    raw_properties = dict(record["props"] or {})
    properties = _to_jsonable(
        {key: value for key, value in raw_properties.items() if key not in _STRIP_KEYS}
    )
    primary_label = labels[0] if labels else "Unknown"
    zone = raw_properties.get("zone") or _LABEL_ZONE.get(primary_label, "")
    return success(
        {
            "key": key,
            "labels": labels,
            "zone": zone,
            "properties": properties,
            "editable_fields": _NODE_EDITABLE_FIELDS.get(primary_label, []),
        }
    )


@router.post("/edge-detail")
def edge_detail(body: EdgeDetailRequest, request: Request):
    """Return full properties and editable-field hints for one edge."""
    query = """
    MATCH (source {key: $source_key})-[rel]->(target {key: $target_key})
    WHERE type(rel) = $rel_type
    RETURN properties(rel) AS props,
           head(labels(source)) AS source_label,
           head(labels(target)) AS target_label,
           coalesce(source.zone, '') AS source_zone,
           coalesce(target.zone, '') AS target_zone
    LIMIT 1
    """
    with graph_session(_driver(request)) as session:
        record = session.run(
            query,
            source_key=body.source_key,
            target_key=body.target_key,
            rel_type=body.rel_type,
        ).single()
    if not record:
        fail(
            "NOT_FOUND",
            f"Edge not found: {body.source_key} -[{body.rel_type}]-> {body.target_key}",
            status_code=404,
        )

    properties = _to_jsonable(
        {
            key: value
            for key, value in dict(record["props"] or {}).items()
            if key not in _STRIP_KEYS
        }
    )
    source_label = record["source_label"] or ""
    target_label = record["target_label"] or ""
    source_zone = record["source_zone"] or _LABEL_ZONE.get(source_label, "")
    target_zone = record["target_zone"] or _LABEL_ZONE.get(target_label, "")
    return success(
        {
            "source_key": body.source_key,
            "target_key": body.target_key,
            "rel_type": body.rel_type,
            "source_label": source_label,
            "target_label": target_label,
            "source_zone": source_zone,
            "target_zone": target_zone,
            "properties": properties,
            "editable_fields": _EDGE_EDITABLE_FIELDS.get(body.rel_type, []),
            "is_cross_graph": bool(source_zone and target_zone and source_zone != target_zone),
        }
    )


@router.post("/search-subgraph")
def search_subgraph(body: SearchSubgraphRequest, request: Request):
    """Search nodes and expand each hit by up to three hops."""
    query = body.query.strip()
    if not query:
        return success({"hit_nodes": [], "nodes": [], "edges": []})

    zone_labels = {
        label
        for label, zone in _LABEL_ZONE.items()
        if zone in (body.scope or ["metadata", "trace", "knowledge"])
    }
    label_filter = ""
    if zone_labels:
        label_filter = " AND (" + " OR ".join(
            f"node:`{label}`" for label in sorted(zone_labels)
        ) + ")"

    if body.match_mode == "exact":
        match_filter = "(node.key = $search_query OR node.name = $search_query)"
    else:
        match_filter = """(
            toLower(coalesce(node.key, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.name, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.canonical_name, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.description, '')) CONTAINS $search_query_lower
            OR any(alias IN coalesce(node.aliases, [])
                   WHERE toLower(toString(alias)) CONTAINS $search_query_lower)
        )"""

    hit_query = f"""
    MATCH (node)
    WHERE {match_filter}{label_filter}
    RETURN node.key AS key, head(labels(node)) AS label,
           coalesce(node.zone, '') AS zone,
           coalesce(node.name, node.canonical_name, node.goal, node.key) AS display_name
    LIMIT $limit
    """
    driver = _driver(request)
    with graph_session(driver) as session:
        hit_rows = session.run(
            hit_query,
            search_query=query,
            search_query_lower=query.lower(),
            limit=body.limit,
        ).data()

    hit_nodes = []
    for row in hit_rows:
        hit_nodes.append(
            {
                "key": row["key"],
                "label": row.get("label", ""),
                "zone": row.get("zone") or _LABEL_ZONE.get(row.get("label", ""), ""),
                "display_name": row.get("display_name", ""),
            }
        )
    if not hit_nodes:
        return success({"hit_nodes": [], "nodes": [], "edges": []})

    nodes_by_key = {node["key"]: node for node in hit_nodes if node.get("key")}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    expand_query = f"""
    MATCH (hit {{key: $hit_key}})
    OPTIONAL MATCH path = (hit)-[*1..{body.hops}]-(neighbor)
    WITH hit, neighbor, relationships(path) AS relationships
    WHERE neighbor IS NOT NULL AND neighbor <> hit
    RETURN neighbor.key AS key, head(labels(neighbor)) AS label,
           coalesce(neighbor.zone, '') AS zone,
           coalesce(neighbor.name, neighbor.canonical_name, neighbor.goal, neighbor.key) AS display_name,
           [rel IN relationships | {{
             source_key: coalesce(startNode(rel).key, ''),
             target_key: coalesce(endNode(rel).key, ''),
             rel_type: type(rel)
           }}] AS edge_info
    LIMIT 200
    """
    with graph_session(driver) as session:
        for hit_key in list(nodes_by_key):
            for row in session.run(expand_query, hit_key=hit_key).data():
                node_key = row.get("key")
                if node_key and node_key not in nodes_by_key:
                    nodes_by_key[node_key] = {
                        "key": node_key,
                        "label": row.get("label", ""),
                        "zone": row.get("zone") or _LABEL_ZONE.get(row.get("label", ""), ""),
                        "display_name": row.get("display_name", ""),
                    }
                for edge in row.get("edge_info") or []:
                    edge_id = (
                        edge.get("source_key", ""),
                        edge.get("target_key", ""),
                        edge.get("rel_type", ""),
                    )
                    if all(edge_id) and edge_id not in seen_edges:
                        seen_edges.add(edge_id)
                        edges.append(
                            {
                                "source_key": edge_id[0],
                                "target_key": edge_id[1],
                                "rel_type": edge_id[2],
                                "properties": {},
                            }
                        )

    return success(
        {"hit_nodes": hit_nodes, "nodes": list(nodes_by_key.values()), "edges": edges}
    )
