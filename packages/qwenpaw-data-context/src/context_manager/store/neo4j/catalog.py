"""Define Neo4j-only constraints and indexes over canonical schema objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from ...schema import GraphSchema, NodeSpec, PropertySpec


class Neo4jIndexKind(StrEnum):
    """Finite Neo4j index implementations supported by schema management."""

    RANGE = "range"
    TEXT = "text"
    FULLTEXT = "fulltext"
    VECTOR = "vector"


@dataclass(frozen=True, slots=True)
class Neo4jConstraint:
    """Declare one physical uniqueness constraint over canonical properties."""

    name: str
    node: NodeSpec
    properties: tuple[PropertySpec, ...]

    def __post_init__(self) -> None:
        """Validate a safe, node-owned physical constraint."""
        if not self.name.isidentifier():
            raise ValueError("constraint name must be a safe identifier")
        if type(self.node) is not NodeSpec:
            raise TypeError("constraint node must be a NodeSpec")
        properties = tuple(self.properties)
        if not properties:
            raise ValueError("constraint requires at least one property")
        if len(properties) != len(set(properties)):
            raise ValueError("constraint properties cannot contain duplicates")
        for prop in properties:
            if (
                not isinstance(prop, PropertySpec)
                or self.node.property(prop.name) is not prop
            ):
                raise ValueError("constraint properties must be canonical for its node")
        object.__setattr__(self, "properties", properties)


@dataclass(frozen=True, slots=True)
class Neo4jIndex:
    """Declare one physical index using fields owned by one logical node."""

    name: str
    kind: Neo4jIndexKind
    node: NodeSpec
    fields: tuple[str, ...]
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate node-local field names and freeze backend options."""
        if not self.name.isidentifier():
            raise ValueError("index name must be a safe identifier")
        if not isinstance(self.kind, Neo4jIndexKind):
            object.__setattr__(self, "kind", Neo4jIndexKind(self.kind))
        if type(self.node) is not NodeSpec:
            raise TypeError("index node must be a NodeSpec")
        fields = tuple(self.fields)
        if not fields:
            raise ValueError("index requires at least one field")
        if not all(isinstance(name, str) and name.isidentifier() for name in fields):
            raise ValueError("index fields must be safe identifiers")
        if len(fields) != len(set(fields)):
            raise ValueError("index fields cannot contain duplicates")
        for name in fields:
            self.node.property(name)
        if (
            self.kind in {Neo4jIndexKind.TEXT, Neo4jIndexKind.VECTOR}
            and len(fields) != 1
        ):
            raise ValueError(f"{self.kind.value} indexes require one field")
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class Neo4jSchemaCatalog:
    """Own the complete physical schema for one exact logical GraphSchema."""

    schema: GraphSchema
    constraints: tuple[Neo4jConstraint, ...]
    indexes: tuple[Neo4jIndex, ...] = ()
    embed_dim: int = 1024

    def __post_init__(self) -> None:
        """Reject incomplete ownership, non-canonical facts, and duplicate names."""
        if type(self.schema) is not GraphSchema:
            raise TypeError("catalog schema must be a GraphSchema")
        if type(self.embed_dim) is not int or self.embed_dim < 1:
            raise ValueError("embed_dim must be a positive integer")
        constraints = tuple(self.constraints)
        indexes = tuple(self.indexes)
        for item in constraints:
            self.schema.require_node(item.node)
        for item in indexes:
            self.schema.require_node(item.node)
        constrained_nodes = {item.node for item in constraints}
        missing = [
            node.name for node in self.schema.nodes if node not in constrained_nodes
        ]
        if missing:
            raise ValueError(
                f"physical schema lacks key constraints for nodes: {missing}"
            )
        for node in self.schema.nodes:
            key_property = node.property("key")
            owned = [
                constraint for constraint in constraints if constraint.node is node
            ]
            if not any(key_property in constraint.properties for constraint in owned):
                raise ValueError(f"physical schema does not constrain {node.name} key")
        names = [item.name for item in (*constraints, *indexes)]
        if len(names) != len(set(names)):
            raise ValueError("physical schema names must be unique")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "indexes", indexes)


__all__ = [
    "Neo4jConstraint",
    "Neo4jIndex",
    "Neo4jIndexKind",
    "Neo4jSchemaCatalog",
]
