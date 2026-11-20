"""Neo4j Graph Center detail queries copied from develop explorer_api.py."""

from __future__ import annotations

from itertools import islice
from types import SimpleNamespace

from ...schema import GRAPH_SCHEMA, DisplayPolicy, PropertyOwner, Zone
from ...store import legacy_reads
from .access import ExplorerAccess
from .display import HIDDEN_FIELDS as _STRIP_KEYS
from .display import json_value as _to_jsonable
from .queries import ExplorerQueries

_LABEL_ZONE = {n.name: n.zone.value for n in GRAPH_SCHEMA.nodes if not n.internal}
_NODE_EDITABLE_FIELDS = {
    n.name: [
        p.name
        for p in n.properties
        if n.zone is Zone.KNOWLEDGE
        and p.owner is PropertyOwner.CALLER
        and p.editable
        and p.display_policy is not DisplayPolicy.NEVER
    ]
    for n in GRAPH_SCHEMA.nodes
}
_EDGE_EDITABLE_FIELDS = {
    e.relationship: [
        p.name
        for p in e.properties
        if e.source.zone is e.target.zone is Zone.KNOWLEDGE
        and p.owner is PropertyOwner.CALLER
        and p.editable
        and p.display_policy is not DisplayPolicy.NEVER
    ]
    for e in GRAPH_SCHEMA.edges
}


def _filter_expand_result(data: dict, *, zone: str | None, label: str | None) -> dict:
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


def _schema_summary_sync(access: ExplorerAccess):
    """Aggregate in the database; only per-type counts cross the connection."""
    queries = ExplorerQueries(access)
    counts = {}
    for row in queries.node_counts():
        counts[row["label"]] = counts.get(row["label"], 0) + row["count"]
    node_labels = []
    for spec in access.specs():
        count = counts.get(spec.name, 0)
        if count:
            node_labels.append(
                {"label": spec.name, "count": count, "zone": spec.zone.value}
            )
    if any(row["label"] == "Entity" for row in node_labels):
        core_count = legacy_reads.count_core_entities(access.store, scope=access.scope)
        if core_count:
            node_labels.append(
                {"label": "Core", "count": core_count, "zone": "_shared"}
            )
    relations = {}
    edge_counts = {}
    for row in queries.edge_counts():
        triple = (row["source_label"], row["relationship"], row["target_label"])
        edge_counts[triple] = edge_counts.get(triple, 0) + row["count"]
    for spec in access.store.schema.edges:
        count = edge_counts.get(
            (spec.source.name, spec.relationship, spec.target.name), 0
        )
        if not count:
            continue
        name = spec.relationship
        source, target = spec.source.zone.value, spec.target.zone.value
        entry = relations.setdefault(
            name,
            {
                "type": name,
                "count": 0,
                "zone": source if source == target else "cross",
                "source_zone": source,
                "target_zone": target,
            },
        )
        entry["count"] += count
    return {
        "node_labels": sorted(node_labels, key=lambda item: item["label"]),
        "relationship_types": sorted(relations.values(), key=lambda item: item["type"]),
    }


def _cross_graph_neighbors_sync(key: str, access: ExplorerAccess, limit: int):
    node = access.node(key)
    if node is None:
        raise LookupError(f"Node not found: {key}")
    result = []
    for other, edge in islice(access.neighbors(node), max(1, min(limit, 500))):
        if other.key == key or other.spec.zone is node.spec.zone:
            continue
        result.append(
            {
                "rel_type": edge.spec.relationship,
                "direction": "out" if edge.source_key == key else "in",
                "other_key": other.key,
                "other_label": other.spec.name,
                "other_name": other.properties.get("name")
                or other.properties.get("canonical_name")
                or "",
                "other_zone": other.spec.zone.value,
            }
        )
    return result


def _node_detail_sync(key: str, access: ExplorerAccess):
    node = access.node(key)
    if node is None:
        raise LookupError(f"Node not found: {key}")
    labels = [node.spec.name]
    if node.spec.name == "Entity" and legacy_reads.count_core_entities(
        access.store, scope=access.scope, key=key
    ):
        labels.append("Core")
    return {
        "key": key,
        "labels": labels,
        "zone": node.spec.zone.value,
        "properties": _to_jsonable(
            {k: v for k, v in node.properties.items() if k not in _STRIP_KEYS}
        ),
        "editable_fields": _NODE_EDITABLE_FIELDS.get(node.spec.name, []),
    }


def _edge_detail_sync(body: SimpleNamespace, access: ExplorerAccess):
    source, target = access.node(body.source_key), access.node(body.target_key)
    if source is not None and target is not None:
        for spec in access.store.schema.edges:
            if (spec.source, spec.relationship, spec.target) != (
                source.spec,
                body.rel_type,
                target.spec,
            ):
                continue
            edge = access.store.get_edges(
                spec, ((source.key, target.key),), scope=access.scope
            )[0]
            if edge is not None:
                return {
                    "source_key": source.key,
                    "target_key": target.key,
                    "rel_type": body.rel_type,
                    "source_label": source.spec.name,
                    "target_label": target.spec.name,
                    "source_zone": source.spec.zone.value,
                    "target_zone": target.spec.zone.value,
                    "properties": _to_jsonable(
                        {
                            k: v
                            for k, v in edge.properties.items()
                            if k not in _STRIP_KEYS
                        }
                    ),
                    "editable_fields": _EDGE_EDITABLE_FIELDS.get(body.rel_type, []),
                    "is_cross_graph": source.spec.zone is not target.spec.zone,
                }
    raise LookupError(
        f"Edge not found: {body.source_key} -[{body.rel_type}]-> {body.target_key}"
    )
