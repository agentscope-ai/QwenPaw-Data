"""Preserve develop's scoped Cypher graph/table results and query timeout.

Runtime lends its Workspace driver to this application adapter. The adapter
owns no connection or lifecycle; it validates and scopes every query before
execution, retaining native values that Store's detached rows cannot represent.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from neo4j import Query
from neo4j.graph import Node, Path, Relationship

from ...cypher.cypher_ast import scope_cypher
from .ds_ownership import node_in_datasource

# Labels whose properties are scoped by datasource selection.
_DATASOURCE_LABELS = frozenset(
    {
        "Domain",
        "DataSource",
        "Table",
        "Column",
        "Dataset",
        "DatasetField",
        "Metric",
        "MetricCaliber",
        "Dimension",
        "VerifiedQuery",
    }
)


class CypherWriteBlocked(ValueError):
    """The Explorer console accepts only develop's read-only subset."""


def _contains_write_keyword(cypher: str) -> bool:
    cleaned = re.sub(
        r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/""",
        " ",
        cypher,
        flags=re.DOTALL,
    )
    return bool(
        re.search(r"\b(create|merge|delete|detach|set|remove)\b", cleaned, re.I)
    )


def _cypher_timeout() -> float:
    timeout = float(os.environ.get("QWENPAW_DATA_CYPHER_TIMEOUT_SECONDS", "10"))
    return max(0.1, min(timeout, 60.0))


def _to_jsonable(obj: Any) -> Any:
    """Match develop's table-cell conversion, including Neo4j temporal values."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(value) for value in obj]
    for name in ("isoformat", "iso_format"):
        formatter = getattr(obj, name, None)
        if callable(formatter):
            try:
                return formatter()
            except Exception:
                pass
    return str(obj)


def _extract_graph(rows: list[dict], columns: list[str]) -> dict | None:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen: set[tuple] = set()

    def add_node(node):
        labels = list(node.labels)
        props = {
            k: v
            for k, v in dict(node).items()
            if k not in ("embedding", "embedding_hash", "signature_emb")
        }
        key = props.get("key", str(node.element_id))
        display = props.get("name", props.get("canonical_name", props.get("goal", key)))
        nodes[key] = {
            "key": key,
            "label": labels[0] if labels else "Unknown",
            "zone": props.get("zone", ""),
            "display_name": str(display),
            "properties": props,
        }

    def process(value):
        if isinstance(value, Node):
            add_node(value)
        elif isinstance(value, Relationship):
            endpoints = []
            for node in (value.start_node, value.end_node):
                if node:
                    add_node(node)
                endpoints.append(
                    dict(node).get("key", str(node.element_id)) if node else ""
                )
            edge_id = (*endpoints, value.type)
            if edge_id not in seen:
                seen.add(edge_id)
                edges.append(
                    {
                        "source_key": endpoints[0],
                        "target_key": endpoints[1],
                        "rel_type": value.type,
                        "properties": {
                            k: v
                            for k, v in dict(value).items()
                            if k not in ("embedding", "embedding_hash")
                        },
                    }
                )
        elif isinstance(value, Path):
            for item in (*value.nodes, *value.relationships):
                process(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                process(item)

    for row in rows:
        for column in columns:
            process(row.get(column))
    return {"nodes": list(nodes.values()), "edges": edges} if nodes or edges else None


def _filter_graph(graph: dict, datasource_id: str) -> dict | None:
    """Apply develop's post-filter to the graph; table rows stay unchanged."""
    nodes = []
    surviving = set()
    for node in graph.get("nodes", []):
        label = node.get("group") or node.get("label") or ""
        if label in _DATASOURCE_LABELS:
            props = dict(node.get("properties") or node.get("props") or node)
            if not props.get("key"):
                props["key"] = node.get("key") or node.get("id") or ""
            if not node_in_datasource(props, datasource_id):
                continue
        nodes.append(node)
        key = node.get("key") or node.get("id") or ""
        if key:
            surviving.add(key)
    edges = [
        edge
        for edge in graph.get("edges", [])
        if (edge.get("source_key") or edge.get("from") or "") in surviving
        and (edge.get("target_key") or edge.get("to") or "") in surviving
    ]
    return {"nodes": nodes, "edges": edges} if nodes or edges else None


class CypherService:
    """Execute the console contract in the authenticated Workspace database."""

    def __init__(self, driver, target):
        self._driver = driver
        self._target = target

    def execute(
        self,
        cypher: str,
        params: dict,
        *,
        user_id: str,
        is_admin: bool,
        limit: int = 200,
        response_format: str = "auto",
        datasource_id: str = "",
        scope: str = "auto",
    ) -> dict:
        if _contains_write_keyword(cypher):
            raise CypherWriteBlocked("只允许只读 Cypher（不支持写操作）")
        query, scope_params = scope_cypher(
            cypher,
            workspace_id=self._target.workspace_id,
            user_id=user_id,
            is_admin=is_admin,
            global_mg_only=scope == "global_mg",
        )
        values = {**params, **scope_params}
        started = time.monotonic()
        options = {"database": self._target.database} if self._target.database else {}
        with self._driver.session(**options) as session:
            result = session.run(Query(query, timeout=_cypher_timeout()), **values)
            columns = list(result.keys())
            records = list(result.fetch(limit + 1))
            native_rows = [dict(zip(columns, record.values())) for record in records]
            serial_rows = [
                dict(zip(columns, (record.data()[key] for key in columns)))
                for record in records
            ]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        columns = columns or (list(native_rows[0]) if native_rows else [])
        # Develop includes the lookahead row in graph/count, but clips table rows.
        graph = (
            _extract_graph(native_rows, columns) if response_format != "table" else None
        )
        if graph and datasource_id.strip():
            graph = _filter_graph(graph, datasource_id.strip())
        return {
            "rows": _to_jsonable(serial_rows[:limit]),
            "count": len(native_rows),
            "columns": columns,
            "graph": graph,
            "summary": {
                "result_type": "graph" if graph else "table",
                "node_count": len(graph["nodes"]) if graph else 0,
                "edge_count": len(graph["edges"]) if graph else 0,
                "labels_hit": list(
                    {node["label"] for node in graph["nodes"]} if graph else []
                ),
                "elapsed_ms": elapsed_ms,
            },
        }
