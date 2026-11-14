"""Test-owned graph declarations for generic Store capabilities."""

from dataclasses import replace

from context_manager.schema import GRAPH_SCHEMA, USER, GraphSchema, KeySpec, KeyStrategy

GENERATED_NODE = replace(
    USER,
    name="GeneratedNode",
    properties=(
        KeySpec(KeyStrategy.GENERATED, "node"),
        *(p for p in USER.properties if p.name != "key"),
    ),
)
GENERATED_SCHEMA = GraphSchema(
    version=1,
    nodes=(*GRAPH_SCHEMA.nodes, GENERATED_NODE),
    edges=GRAPH_SCHEMA.edges,
)
