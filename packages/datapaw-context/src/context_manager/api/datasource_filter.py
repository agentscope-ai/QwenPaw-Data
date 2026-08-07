"""Datasource post-filter for graph API responses.

MG and TG nodes carry ``datasource_id``; KG nodes are global.
When a caller supplies a ``datasource_id``, MG/TG nodes that belong
to a different datasource are removed, along with edges whose
endpoints were filtered out.
"""
from __future__ import annotations

from typing import Any

from .retrieval import _METADATA_LAYER_LABELS, _TRACE_LAYER_LABELS

_DATASOURCE_FILTERED_LABELS = frozenset(
    _METADATA_LAYER_LABELS + _TRACE_LAYER_LABELS
)

def filter_graph_by_datasource(
    graph: dict[str, Any],
    datasource_id: str,
) -> dict[str, Any] | None:
    """Remove MG/TG nodes not matching *datasource_id*; keep KG nodes.

    KG nodes (label not in ``_DATASOURCE_FILTERED_LABELS``) always pass. MG/TG
    nodes must carry a ``datasource_id`` equal to *datasource_id*; a different
    value **or a missing property** (e.g. Strategy / legacy trace nodes) is
    filtered out — no fallback for un-stamped nodes.

    Returns ``None`` when every node is filtered out.
    """
    ds = (datasource_id or "").strip()
    if not ds:
        return graph

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    surviving_keys: set[str] = set()
    filtered_nodes: list[dict] = []
    for node in nodes:
        # Explorer nodes use ``label`` as display text and ``group`` as type;
        # Cypher nodes expose the type directly in ``label``.
        label = node.get("group") or node.get("label") or ""
        if label in _DATASOURCE_FILTERED_LABELS:
            props = node.get("properties") or node.get("props") or node
            node_ds = str(props.get("datasource_id") or "").strip()
            # Require an exact match: nodes on a different datasource *and*
            # nodes with no datasource_id are dropped (no legacy fallback).
            if node_ds != ds:
                continue
        filtered_nodes.append(node)
        key = node.get("key") or node.get("id") or ""
        if key:
            surviving_keys.add(key)

    filtered_edges = [
        e for e in edges
        if (e.get("source_key") or e.get("from") or "") in surviving_keys
        and (e.get("target_key") or e.get("to") or "") in surviving_keys
    ]

    if not filtered_nodes and not filtered_edges:
        return None
    return {"nodes": filtered_nodes, "edges": filtered_edges}
