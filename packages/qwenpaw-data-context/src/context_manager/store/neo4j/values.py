"""Detach native Neo4j values without losing graph type information."""

from typing import Any

from neo4j import Record
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Time

from ..models import GraphPropertyMap


def _detach_query_value(value: Any) -> Any:
    """Preserve relationship properties and path direction in native query rows."""
    if isinstance(value, Node):
        return GraphPropertyMap(
            _detach_value(dict(value)), kind="node", types=tuple(value.labels)
        )
    if isinstance(value, Relationship):
        return {
            "relationship": value.type,
            "source_element_id": value.start_node.element_id,
            "target_element_id": value.end_node.element_id,
            "properties": GraphPropertyMap(
                _detach_value(dict(value)), kind="edge", types=(value.type,)
            ),
        }
    if isinstance(value, Path):
        return {
            "nodes": [_detach_query_value(node) for node in value.nodes],
            "edges": [_detach_query_value(edge) for edge in value.relationships],
        }
    if isinstance(value, dict):
        return {name: _detach_query_value(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detach_query_value(item) for item in value]
    return _detach_value(value)


def _detach_value(value: Any) -> Any:
    """Detach property containers once, retaining the driver's graph export format."""
    if type(value) in (float, int, str, bool, bytes, type(None)):
        return value
    if isinstance(value, (DateTime, Date, Time)):
        return value.to_native()
    if isinstance(value, dict):
        return {name: _detach_value(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_detach_value(item) for item in value]
    # Nodes, relationships, paths and spatial values retain Record.data() semantics.
    exported = Record((("value", value),)).data()["value"]
    if isinstance(exported, (dict, list)):
        return _detach_value(exported)
    return exported
