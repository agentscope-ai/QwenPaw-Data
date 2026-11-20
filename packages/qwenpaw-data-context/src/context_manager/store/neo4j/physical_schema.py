"""Declare the complete Neo4j physical schema over canonical graph objects."""

from __future__ import annotations

from collections.abc import Mapping

from ...schema import GRAPH_SCHEMA, NodeSpec
from ..runtime import GraphRuntime
from .catalog import (
    Neo4jConstraint,
    Neo4jIndex,
    Neo4jIndexKind,
    Neo4jSchemaCatalog,
)

# Retain ID indexes for datasource-bounded reads. Exact vector search also
# needs a scope-only index: optional trailing IDs can be absent from nodes.
_SCOPE_ID_FIELDS = ("workspace_id", "owner_id")


def _key_constraint(node: NodeSpec) -> Neo4jConstraint:
    """Derive one label-local uniqueness constraint for the fixed node key."""
    return Neo4jConstraint(
        name=f"cm_{node.name.lower()}_key",
        node=node,
        properties=(node.property("key"),),
    )


NEO4J_CONSTRAINTS = tuple(_key_constraint(node) for node in GRAPH_SCHEMA.nodes)


def _indexes_for(node: NodeSpec) -> tuple[Neo4jIndex, ...]:
    """Derive Neo4j indexes from one canonical logical node declaration."""
    property_names = tuple(property_.name for property_ in node.properties)
    # Key-based CRUD and edge endpoint lookup use the UNIQUE(key) constraint.
    # This separate RANGE access path serves scope-bounded list/overview reads.
    id_fields = (
        *(name for name in _SCOPE_ID_FIELDS if name in property_names),
        *(
            name
            for name in property_names
            if (name == "id" or name.endswith("_id"))
            and name not in _SCOPE_ID_FIELDS
            # Card IDs are provenance, not scope/identity lookup fields. Keep
            # existing composite indexes stable when adding feedback support.
            and name != "card_client_id"
        ),
    )
    constrained_fields = {
        tuple(property_.name for property_ in constraint.properties)
        for constraint in NEO4J_CONSTRAINTS
        if constraint.node is node
    }
    prefix = f"cm_{node.name.lower()}"
    indexes: list[Neo4jIndex] = []
    if id_fields and id_fields not in constrained_fields:
        indexes.append(
            Neo4jIndex(
                f"{prefix}_ids",
                Neo4jIndexKind.RANGE,
                node,
                id_fields,
            )
        )
    if node.fulltext_fields:
        indexes.append(
            Neo4jIndex(
                f"{prefix}_fulltext",
                Neo4jIndexKind.FULLTEXT,
                node,
                node.fulltext_fields,
                {"indexConfig": {"fulltext.analyzer": "cjk"}},
            )
        )
    if node.embedding_fields and id_fields != _SCOPE_ID_FIELDS:
        indexes.append(
            Neo4jIndex(
                f"{prefix}_scope",
                Neo4jIndexKind.RANGE,
                node,
                _SCOPE_ID_FIELDS,
            )
        )
    return tuple(indexes)


NEO4J_INDEXES = tuple(
    index for node in GRAPH_SCHEMA.nodes for index in _indexes_for(node)
)

NEO4J_PHYSICAL_SCHEMA = Neo4jSchemaCatalog(
    schema=GRAPH_SCHEMA,
    constraints=NEO4J_CONSTRAINTS,
    indexes=NEO4J_INDEXES,
)


def ensure_schema(
    runtime: GraphRuntime,
    catalog: Neo4jSchemaCatalog = NEO4J_PHYSICAL_SCHEMA,
    *,
    timeout_seconds: int = 60,
) -> None:
    """Prepare a database explicitly before admitting Workspace traffic."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 600
    ):
        raise ValueError("timeout_seconds must be an integer from 1 to 600")
    for constraint in catalog.constraints:
        runtime.write(
            _constraint_query(constraint), {}, operation="ensure_schema_constraint"
        )
    for index in catalog.indexes:
        query, parameters = _index_query(index)
        runtime.write(query, parameters, operation="ensure_schema_index")
    runtime.read(
        "UNWIND $index_names AS name CALL db.awaitIndex(name, $timeout_seconds)",
        {
            "index_names": [
                item.name for item in (*catalog.constraints, *catalog.indexes)
            ],
            "timeout_seconds": timeout_seconds,
        },
        operation="await_schema_online",
    )


def _constraint_query(constraint: Neo4jConstraint) -> str:
    expressions = ", ".join(f"n.{prop.name}" for prop in constraint.properties)
    if len(constraint.properties) > 1:
        expressions = f"({expressions})"
    return (
        f"CREATE CONSTRAINT {constraint.name} IF NOT EXISTS "
        f"FOR (n:{constraint.node.name}) REQUIRE {expressions} IS UNIQUE"
    )


def _index_query(index: Neo4jIndex) -> tuple[str, Mapping[str, object]]:
    expressions = ", ".join(f"n.{field}" for field in index.fields)
    if index.kind is Neo4jIndexKind.FULLTEXT:
        query = (
            f"CREATE FULLTEXT INDEX {index.name} IF NOT EXISTS "
            f"FOR (n:{index.node.name}) ON EACH [{expressions}]"
        )
    else:
        query = (
            f"CREATE {index.kind.value.upper()} INDEX {index.name} IF NOT EXISTS "
            f"FOR (n:{index.node.name}) ON ({expressions})"
        )
    options = dict(index.options)
    return (
        (f"{query} OPTIONS $options", {"options": options}) if options else (query, {})
    )


__all__ = ["NEO4J_PHYSICAL_SCHEMA", "ensure_schema"]
