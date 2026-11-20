"""Define the single canonical graph-schema object model."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import uuid4

from ..utils.validation import require_trimmed_text


class CrudOperation(StrEnum):
    """Finite operations supported by a node or exact edge type."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"


class DisplayPolicy(StrEnum):
    """Control which properties are exposed to users."""

    NEVER = "never"  # Never exposed to users; may flow through internal algorithms.
    CONDITIONAL = "conditional"  # Exposed only when users need detailed information.
    ALWAYS = "always"  # Exposed to users in every display context.


class Zone(StrEnum):
    """Enumerate every logical graph zone from one canonical vocabulary."""

    METADATA = "metadata"
    KNOWLEDGE = "knowledge"
    USER = "user"
    INTERNAL = "_internal"


class PropertyOwner(StrEnum):
    """Identify the authority that supplies a persisted property."""

    CALLER = "caller"
    SCOPE = "scope"
    STORE = "store"


class ValueKind(StrEnum):
    """Database-neutral logical value categories."""

    ANY = "any"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    FLOAT = "float"
    INTEGER = "integer"
    JSON = "json"
    TEXT = "text"
    TEXT_LIST = "text_list"
    VECTOR = "vector"


class KeyStrategy(StrEnum):
    """Select how the Store derives a node's immutable key."""

    GENERATED = "generated"
    DETERMINISTIC = "deterministic"


def _text(value: str, field_name: str) -> str:
    """Return non-empty trimmed schema text."""
    value = require_trimmed_text(value, field_name)
    if not value.isidentifier():
        raise ValueError(f"{field_name} must be a safe identifier")
    return value


def _enum_set(
    values: Iterable[CrudOperation | str],
) -> frozenset[CrudOperation]:
    """Canonicalize an operation collection without duplicates."""
    raw = tuple(values)
    parsed = tuple(CrudOperation(value) for value in raw)
    if len(parsed) != len(set(parsed)):
        raise ValueError("operations contain duplicate values")
    return frozenset(parsed)


@dataclass(frozen=True, slots=True)
class PropertySpec:
    """Declare one logical property once for every downstream consumer."""

    name: str
    kind: ValueKind = ValueKind.ANY
    editable: bool = True
    display_policy: DisplayPolicy = DisplayPolicy.CONDITIONAL
    nullable: bool = True
    owner: PropertyOwner = PropertyOwner.CALLER

    def __post_init__(self) -> None:
        """Validate one immutable property declaration."""
        object.__setattr__(self, "name", _text(self.name, "property name"))
        if not isinstance(self.kind, ValueKind):
            object.__setattr__(self, "kind", ValueKind(self.kind))
        if not isinstance(self.editable, bool):
            raise TypeError("property editable must be boolean")
        if not isinstance(self.display_policy, DisplayPolicy):
            object.__setattr__(
                self,
                "display_policy",
                DisplayPolicy(self.display_policy),
            )
        if not isinstance(self.owner, PropertyOwner):
            object.__setattr__(self, "owner", PropertyOwner(self.owner))
        if not isinstance(self.nullable, bool):
            raise TypeError("property nullable must be boolean")
        if self.owner is not PropertyOwner.CALLER and self.editable:
            raise ValueError("managed properties cannot be caller-editable")


@dataclass(frozen=True, slots=True)
class KeySpec(PropertySpec):
    """Declare the Store-managed immutable ``key`` and its generation rule."""

    name: str = field(init=False, default="key")
    kind: ValueKind = field(init=False, default=ValueKind.TEXT)
    editable: bool = field(init=False, default=False)
    display_policy: DisplayPolicy = field(
        init=False,
        default=DisplayPolicy.ALWAYS,
    )
    nullable: bool = field(init=False, default=False)
    owner: PropertyOwner = field(init=False, default=PropertyOwner.STORE)
    strategy: KeyStrategy
    prefix: str
    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the fixed property contract and strategy-specific inputs."""
        PropertySpec.__post_init__(self)
        if not isinstance(self.strategy, KeyStrategy):
            object.__setattr__(self, "strategy", KeyStrategy(self.strategy))
        object.__setattr__(self, "prefix", _text(self.prefix, "key prefix"))
        if isinstance(self.parts, (str, bytes)):
            raise TypeError("key parts must be a collection of property names")
        parts = tuple(_text(part, "key part") for part in self.parts)
        if len(parts) != len(set(parts)):
            raise ValueError("key parts contain duplicate properties")
        if "key" in parts:
            raise ValueError("key parts cannot reference key itself")
        if self.strategy is KeyStrategy.GENERATED and parts:
            raise ValueError("generated keys cannot declare parts")
        object.__setattr__(self, "parts", parts)

    def generate(self, values: Mapping[str, object]) -> str:
        """Generate one prefixed key from validated node values."""
        if not isinstance(values, Mapping):
            raise TypeError("key values must be a mapping")
        if self.strategy is KeyStrategy.GENERATED:
            return f"{self.prefix}_{uuid4().hex}"
        if not self.parts:
            raise ValueError("deterministic key parts are not configured")
        missing = [
            name for name in self.parts if name not in values or values[name] is None
        ]
        if missing:
            raise ValueError(f"deterministic key values are missing: {missing}")
        return ":".join((self.prefix, *(str(values[name]) for name in self.parts)))


def _property_document(prop: PropertySpec) -> dict[str, object]:
    """Export one property, including fields owned by specialized declarations."""
    document: dict[str, object] = {
        "name": prop.name,
        "kind": prop.kind.value,
        "editable": prop.editable,
        "display_policy": prop.display_policy.value,
        "nullable": prop.nullable,
        "owner": prop.owner.value,
    }
    if isinstance(prop, KeySpec):
        document.update(
            strategy=prop.strategy.value,
            prefix=prop.prefix,
            parts=list(prop.parts),
        )
    return document


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """Declare one canonical logical node type."""

    name: str
    zone: Zone
    operations: frozenset[CrudOperation]
    properties: tuple[PropertySpec, ...]
    fulltext_fields: tuple[str, ...] = ()
    embedding_fields: tuple[str, ...] = ()
    internal: bool = False  # Persistence bookkeeping, omitted from public topology and graph browsing.
    _properties_by_name: Mapping[str, PropertySpec] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validate local node invariants and build an immutable property index."""
        object.__setattr__(self, "name", _text(self.name, "node name"))
        if type(self.zone) is not Zone:
            raise TypeError("node zone must be a Zone")
        if type(self.internal) is not bool:
            raise TypeError("node internal must be boolean")
        properties = tuple(self.properties)
        if not properties or not all(
            isinstance(item, PropertySpec) for item in properties
        ):
            raise TypeError("node properties must contain PropertySpec values")
        names = tuple(item.name for item in properties)
        if len(names) != len(set(names)):
            raise ValueError(f"node {self.name} contains duplicate properties")
        by_name = {item.name: item for item in properties}
        key_specs = tuple(item for item in properties if isinstance(item, KeySpec))
        if len(key_specs) != 1:
            raise ValueError(f"node {self.name} requires exactly one KeySpec")
        key_spec = key_specs[0]
        unknown_key_parts = set(key_spec.parts) - set(names)
        if unknown_key_parts:
            raise ValueError(
                f"node {self.name} key references unknown properties: "
                f"{sorted(unknown_key_parts)}"
            )
        nullable_key_parts = [name for name in key_spec.parts if by_name[name].nullable]
        if nullable_key_parts:
            raise ValueError(
                f"node {self.name} key properties cannot be nullable: "
                f"{nullable_key_parts}"
            )
        editable_key_parts = [name for name in key_spec.parts if by_name[name].editable]
        if editable_key_parts:
            raise ValueError(
                f"node {self.name} key properties cannot be editable: "
                f"{editable_key_parts}"
            )
        fulltext_fields = self._text_fields(
            self.fulltext_fields,
            "fulltext_fields",
            by_name,
        )
        embedding_fields = self._text_fields(
            self.embedding_fields,
            "embedding_fields",
            by_name,
        )
        if embedding_fields:
            embedding = by_name.get("embedding")
            if embedding is None or embedding.kind is not ValueKind.VECTOR:
                raise ValueError(
                    f"node {self.name} embedding_fields require "
                    "an embedding VECTOR property"
                )
            embedding_hash = by_name.get("embedding_hash")
            if embedding_hash is None or embedding_hash.kind is not ValueKind.TEXT:
                raise ValueError(
                    f"node {self.name} embedding_fields require "
                    "an embedding_hash TEXT property"
                )
        object.__setattr__(self, "properties", properties)
        object.__setattr__(self, "fulltext_fields", fulltext_fields)
        object.__setattr__(self, "embedding_fields", embedding_fields)
        object.__setattr__(self, "operations", _enum_set(self.operations))
        object.__setattr__(
            self,
            "_properties_by_name",
            MappingProxyType(by_name),
        )

    def _text_fields(
        self,
        fields: Iterable[str],
        field_name: str,
        properties: Mapping[str, PropertySpec],
    ) -> tuple[str, ...]:
        """Validate one ordered node-level collection of searchable fields."""
        if isinstance(fields, (str, bytes)):
            raise TypeError(f"node {field_name} must be a collection")
        normalized = tuple(_text(name, field_name) for name in fields)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"node {self.name} {field_name} contains duplicates")
        unknown = sorted(set(normalized) - set(properties))
        if unknown:
            raise ValueError(
                f"node {self.name} {field_name} references unknown properties: "
                f"{unknown}"
            )
        unsupported = [
            name
            for name in normalized
            if properties[name].kind not in {ValueKind.TEXT, ValueKind.TEXT_LIST}
        ]
        if unsupported:
            raise ValueError(
                f"node {self.name} {field_name} properties must be text: {unsupported}"
            )
        return normalized

    def property(self, name: str) -> PropertySpec:
        """Return one canonical property owned by this node."""
        try:
            return self._properties_by_name[name]
        except KeyError as exc:
            raise ValueError(f"unknown {self.name} property: {name!r}") from exc


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """Declare one canonical endpoint-closed logical edge type."""

    source: NodeSpec
    relationship: str
    target: NodeSpec
    operations: frozenset[CrudOperation]
    properties: tuple[PropertySpec, ...] = ()
    _properties_by_name: Mapping[str, PropertySpec] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validate local edge invariants and build its property index."""
        if type(self.source) is not NodeSpec or type(self.target) is not NodeSpec:
            raise TypeError("edge endpoints must be NodeSpec values")
        object.__setattr__(
            self,
            "relationship",
            _text(self.relationship, "relationship"),
        )
        properties = tuple(self.properties)
        if not all(type(item) is PropertySpec for item in properties):
            raise TypeError("edge properties must contain PropertySpec values")
        names = tuple(item.name for item in properties)
        if len(names) != len(set(names)):
            raise ValueError(f"edge {self.triple} contains duplicate properties")
        by_name = {item.name: item for item in properties}
        object.__setattr__(self, "properties", properties)
        object.__setattr__(self, "operations", _enum_set(self.operations))
        object.__setattr__(
            self,
            "_properties_by_name",
            MappingProxyType(by_name),
        )

    @property
    def triple(self) -> tuple[str, str, str]:
        """Return the exact endpoint-qualified relationship key."""
        return self.source.name, self.relationship, self.target.name

    def property(self, name: str) -> PropertySpec:
        """Return one canonical property owned by this edge."""
        try:
            return self._properties_by_name[name]
        except KeyError as exc:
            raise ValueError(f"unknown {self.triple} property: {name!r}") from exc


SchemaOwner = NodeSpec | EdgeSpec


@dataclass(frozen=True, slots=True)
class GraphSchema:
    """Own the single canonical graph declarations, validation, and indexes."""

    version: int
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    _nodes_by_name: Mapping[str, NodeSpec] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _edges_by_triple: Mapping[tuple[str, str, str], EdgeSpec] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validate the complete graph and register canonical object identities."""
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("schema version must be a positive integer")
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        if not nodes or not all(type(node) is NodeSpec for node in nodes):
            raise TypeError("schema nodes must contain NodeSpec values")
        if not all(type(edge) is EdgeSpec for edge in edges):
            raise TypeError("schema edges must contain EdgeSpec values")
        nodes_by_name = {node.name: node for node in nodes}
        if len(nodes_by_name) != len(nodes):
            raise ValueError("schema contains duplicate node names")
        for edge in edges:
            if nodes_by_name.get(edge.source.name) is not edge.source:
                raise ValueError(f"edge {edge.triple} has a non-canonical source")
            if nodes_by_name.get(edge.target.name) is not edge.target:
                raise ValueError(f"edge {edge.triple} has a non-canonical target")
        edges_by_triple = {edge.triple: edge for edge in edges}
        if len(edges_by_triple) != len(edges):
            raise ValueError("schema contains duplicate edge triples")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(
            self,
            "_nodes_by_name",
            MappingProxyType(nodes_by_name),
        )
        object.__setattr__(
            self,
            "_edges_by_triple",
            MappingProxyType(edges_by_triple),
        )

    @property
    def zones(self) -> tuple[Zone, ...]:
        """Expose the complete zone vocabulary without another declaration list."""
        return tuple(Zone)

    @property
    def nodes_by_name(self) -> Mapping[str, NodeSpec]:
        """Expose the immutable canonical node index."""
        return self._nodes_by_name

    @property
    def edges_by_triple(self) -> Mapping[tuple[str, str, str], EdgeSpec]:
        """Expose the immutable canonical edge index."""
        return self._edges_by_triple

    def node(self, owner: str | NodeSpec) -> NodeSpec:
        """Resolve a name or require one canonical node object."""
        if isinstance(owner, str):
            try:
                return self._nodes_by_name[owner]
            except KeyError as exc:
                raise ValueError(f"unknown node: {owner!r}") from exc
        return self.require_node(owner)

    def edge(
        self,
        owner: EdgeSpec | tuple[str, str, str],
    ) -> EdgeSpec:
        """Resolve a triple or require one canonical edge object."""
        if isinstance(owner, tuple):
            if len(owner) != 3 or not all(isinstance(item, str) for item in owner):
                raise TypeError("edge key must be a text triple")
            try:
                return self._edges_by_triple[owner]
            except KeyError as exc:
                raise ValueError(f"unknown edge: {owner!r}") from exc
        return self.require_edge(owner)

    def require_node(self, node: NodeSpec) -> NodeSpec:
        """Reject another instance even when it has the same node name."""
        if type(node) is not NodeSpec:
            raise TypeError("node must be a NodeSpec")
        canonical = self.node(node.name)
        if canonical is not node:
            raise ValueError("node is not canonical for this GraphSchema")
        return node

    def require_edge(self, edge: EdgeSpec) -> EdgeSpec:
        """Reject another instance even when it has the same edge triple."""
        if type(edge) is not EdgeSpec:
            raise TypeError("edge must be an EdgeSpec")
        canonical = self.edge(edge.triple)
        if canonical is not edge:
            raise ValueError("edge is not canonical for this GraphSchema")
        return edge

    def validate_operation(
        self,
        owner: SchemaOwner,
        operation: CrudOperation | str,
        properties: Iterable[str] = (),
    ) -> None:
        """Validate canonical ownership, capability, and caller editability."""
        canonical: SchemaOwner
        if isinstance(owner, NodeSpec):
            canonical = self.require_node(owner)
        elif isinstance(owner, EdgeSpec):
            canonical = self.require_edge(owner)
        else:
            raise TypeError("owner must be a NodeSpec or EdgeSpec")
        requested_operation = CrudOperation(operation)
        if requested_operation not in canonical.operations:
            raise PermissionError(
                f"{requested_operation.value} is denied for schema owner"
            )
        names = tuple(properties)
        if any(not isinstance(name, str) for name in names):
            raise TypeError("property names must be text")
        known = {item.name: item for item in canonical.properties}
        unknown = set(names) - set(known)
        if unknown:
            raise ValueError(f"unknown properties: {sorted(unknown)}")
        if requested_operation is CrudOperation.CREATE:
            denied = {
                name for name in names if known[name].owner is not PropertyOwner.CALLER
            }
            if denied:
                raise PermissionError(
                    f"properties are supplied by the Store: {sorted(denied)}"
                )
        if requested_operation is CrudOperation.UPDATE:
            denied = {
                name
                for name in names
                if not known[name].editable
                or known[name].owner is not PropertyOwner.CALLER
            }
            if denied:
                raise PermissionError(f"properties are not editable: {sorted(denied)}")

    def validate_values(
        self,
        owner: SchemaOwner,
        values: Mapping[str, object],
        *,
        complete: bool = False,
    ) -> None:
        """Validate property names, nullability, logical kinds, and completeness."""
        if isinstance(owner, NodeSpec):
            canonical: SchemaOwner = self.require_node(owner)
        elif isinstance(owner, EdgeSpec):
            canonical = self.require_edge(owner)
        else:
            raise TypeError("owner must be a NodeSpec or EdgeSpec")
        if not isinstance(values, Mapping) or any(
            not isinstance(name, str) for name in values
        ):
            raise TypeError("property values must be a text-keyed mapping")
        properties = {prop.name: prop for prop in canonical.properties}
        unknown = set(values) - set(properties)
        if unknown:
            raise ValueError(f"unknown properties: {sorted(unknown)}")
        for name, value in values.items():
            prop = properties[name]
            if value is None:
                if not prop.nullable:
                    raise ValueError(f"property {name!r} cannot be null")
                continue
            if not self._matches_kind(value, prop.kind):
                raise TypeError(
                    f"property {name!r} must match logical kind {prop.kind.value}"
                )
        if complete:
            missing = {
                prop.name
                for prop in canonical.properties
                if not prop.nullable and prop.name not in values
            }
            if missing:
                raise ValueError(f"required properties are missing: {sorted(missing)}")

    @staticmethod
    def _matches_kind(value: object, kind: ValueKind) -> bool:
        """Check one value against the finite logical kind vocabulary."""
        if kind in {ValueKind.ANY, ValueKind.JSON}:
            return True
        if kind is ValueKind.BOOLEAN:
            return isinstance(value, bool)
        if kind is ValueKind.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if kind is ValueKind.FLOAT:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        if kind is ValueKind.TEXT:
            return isinstance(value, str)
        if kind is ValueKind.DATETIME:
            return isinstance(value, (str, date, datetime))
        if kind is ValueKind.TEXT_LIST:
            return (
                isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                and all(isinstance(item, str) for item in value)
            )
        if kind is ValueKind.VECTOR:
            return (
                isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                and all(
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and math.isfinite(float(item))
                    for item in value
                )
            )
        return False

    def get_overview(self) -> dict[str, object]:
        """Summarize topology, displayable property names and search fields."""
        return {
            "version": self.version,
            "zones": [zone.value for zone in self.zones if zone is not Zone.INTERNAL],
            "nodes": [
                {
                    "name": node.name,
                    "zone": node.zone.value,
                    "properties": [
                        prop.name
                        for prop in node.properties
                        if prop.display_policy is not DisplayPolicy.NEVER
                    ],
                    "fulltext_fields": list(node.fulltext_fields),
                    "embedding_fields": list(node.embedding_fields),
                }
                for node in self.nodes
                if not node.internal
            ],
            "edges": [
                {
                    "source": edge.source.name,
                    "relationship": edge.relationship,
                    "target": edge.target.name,
                    "properties": [
                        prop.name
                        for prop in edge.properties
                        if prop.display_policy is not DisplayPolicy.NEVER
                    ],
                }
                for edge in self.edges
            ],
        }

    @property
    def fingerprint(self) -> str:
        """Return a deterministic digest for migration and deployment gates."""
        payload = json.dumps(
            self.to_document(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_document(self) -> dict[str, object]:
        """Export a read-only deployment document; it is not an authoring path."""
        return {
            "version": self.version,
            "zones": [zone.value for zone in self.zones],
            "nodes": [
                {
                    "name": node.name,
                    "zone": node.zone.value,
                    "operations": sorted(
                        operation.value for operation in node.operations
                    ),
                    "internal": node.internal,
                    "fulltext_fields": list(node.fulltext_fields),
                    "embedding_fields": list(node.embedding_fields),
                    "properties": [
                        _property_document(prop) for prop in node.properties
                    ],
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "source": edge.source.name,
                    "relationship": edge.relationship,
                    "target": edge.target.name,
                    "operations": sorted(
                        operation.value for operation in edge.operations
                    ),
                    "properties": [
                        _property_document(prop) for prop in edge.properties
                    ],
                }
                for edge in self.edges
            ],
        }


__all__ = [
    "CrudOperation",
    "DisplayPolicy",
    "EdgeSpec",
    "GraphSchema",
    "KeySpec",
    "KeyStrategy",
    "NodeSpec",
    "PropertyOwner",
    "PropertySpec",
    "SchemaOwner",
    "ValueKind",
]
