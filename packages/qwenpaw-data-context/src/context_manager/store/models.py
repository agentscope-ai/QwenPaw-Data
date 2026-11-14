"""Define detached values crossing the database-neutral graph Store boundary.

Drafts and records describe opposite sides of one persistence operation. A Draft
is a schema-bound, request-local value that has not yet received a Store key. A
Record is a detached snapshot returned after an exact Store read or write and
therefore carries persisted endpoint keys. Neither type owns a database session,
driver value, or live transaction state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from ..schema import (
    CrudOperation,
    EdgeSpec,
    GraphSchema,
    NodeSpec,
    PropertyOwner,
)
from ..utils.immutable import freeze_mapping
from ..utils.validation import require_trimmed_text


@dataclass(frozen=True, slots=True)
class QueryDiagnostic:
    """Carry native query diagnostics safe for the submitting caller.

    Backend codes and messages are preserved regardless of error category.
    Position, when supplied, is a one-based character offset in the submitted statement.
    Driver context, connection details and bound parameters do not belong here.
    """

    code: str
    message: str
    position: int | None = None


class StoreError(RuntimeError):
    """Hide backend failures behind a stable persistence error."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        code: str | None = None,
        retryable: bool = False,
        query_diagnostic: QueryDiagnostic | None = None,
    ) -> None:
        """Create one sanitized Store failure."""
        super().__init__(message)
        self.operation = operation
        self.code = code
        self.retryable = retryable
        self.query_diagnostic = query_diagnostic


class GraphPropertyMap(dict[str, object]):
    """Detach native graph properties while retaining types for public projection.

    Native rows remain ordinary mapping-compatible values. Type metadata is not
    a stored property or a driver reference, and is not serialized into the map.
    """

    def __init__(
        self,
        properties: Mapping[str, object],
        *,
        kind: Literal["node", "edge"],
        types: tuple[str, ...],
    ) -> None:
        super().__init__(properties)
        self.kind = kind
        self.types = types


@dataclass(frozen=True, slots=True)
class NodeDraft:
    """Carry one immutable canonical node before persistence.

    ``ref`` is a plan-local data-flow name, not a graph key. Extraction and
    planning algorithms use it to relate an :class:`EdgeDraft` to this node
    before ``GraphStore.create_nodes`` can generate the persisted key. A planner
    may instead resolve the same ref to an existing :class:`NodeRecord`; a Draft
    therefore does not imply that a node will be created.

    ``properties`` contains only caller-owned canonical facts. Store-owned key
    and Scope-owned tenant, Workspace, owner, zone, or datasource values cannot
    be supplied through a Draft. ``transient`` carries request-local algorithm
    state such as extraction lineage or resolution status. It is intentionally
    excluded from :meth:`output` and is never persisted by the Store.
    """

    spec: NodeSpec
    ref: str
    properties: Mapping[str, object]
    transient: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Freeze request-local values and reject Store-managed properties."""
        if type(self.spec) is not NodeSpec:
            raise TypeError("node draft spec must be a NodeSpec")
        object.__setattr__(
            self,
            "ref",
            require_trimmed_text(self.ref, "node ref"),
        )
        properties = freeze_mapping(
            self.properties,
            field_name="draft properties",
            key_field_name="draft property",
        )
        managed = {
            name
            for name in properties
            if self.spec.property(name).owner is not PropertyOwner.CALLER
        }
        if managed:
            raise PermissionError(
                f"properties are supplied by the Store: {sorted(managed)}"
            )
        object.__setattr__(self, "properties", properties)
        object.__setattr__(
            self,
            "transient",
            freeze_mapping(
                self.transient,
                field_name="draft transient values",
                key_field_name="draft transient property",
            ),
        )

    @classmethod
    def build(
        cls,
        schema: GraphSchema,
        spec: NodeSpec,
        ref: str,
        properties: Mapping[str, object],
        *,
        transient: Mapping[str, object] | None = None,
    ) -> NodeDraft:
        """Validate one canonical create payload and return its detached Draft."""
        schema.require_node(spec)
        schema.validate_operation(spec, CrudOperation.CREATE, properties)
        schema.validate_values(spec, properties)
        return cls(spec, ref, properties, transient or {})

    def output(self) -> Mapping[str, object]:
        """Return caller-owned Store input without ref or transient metadata."""
        caller_fields = {
            property_spec.name
            for property_spec in self.spec.properties
            if property_spec.owner is PropertyOwner.CALLER
        }
        return MappingProxyType(
            {
                name: value
                for name, value in self.properties.items()
                if name in caller_fields
            }
        )


@dataclass(frozen=True, slots=True)
class EdgeDraft:
    """Carry one immutable canonical edge before endpoint keys exist.

    ``source_ref`` and ``target_ref`` name NodeDraft values in the same request
    or transaction plan. They are deliberately not persisted node keys. A Store
    transaction executor resolves both refs after earlier node operations have
    created a node or bound an existing NodeRecord. The Draft itself therefore
    does not prove that either endpoint exists or that the edge will be written.

    ``properties`` contains caller-owned edge facts. ``transient`` is unsaved
    request-local algorithm state and never appears in :meth:`output`.
    """

    spec: EdgeSpec
    source_ref: str
    target_ref: str
    properties: Mapping[str, object]
    transient: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Freeze one edge candidate and require explicit endpoint refs."""
        if type(self.spec) is not EdgeSpec:
            raise TypeError("edge draft spec must be an EdgeSpec")
        object.__setattr__(
            self,
            "source_ref",
            require_trimmed_text(self.source_ref, "edge source_ref"),
        )
        object.__setattr__(
            self,
            "target_ref",
            require_trimmed_text(self.target_ref, "edge target_ref"),
        )
        object.__setattr__(
            self,
            "properties",
            freeze_mapping(
                self.properties,
                field_name="draft properties",
                key_field_name="draft property",
            ),
        )
        object.__setattr__(
            self,
            "transient",
            freeze_mapping(
                self.transient,
                field_name="draft transient values",
                key_field_name="draft transient property",
            ),
        )

    @classmethod
    def build(
        cls,
        schema: GraphSchema,
        spec: EdgeSpec,
        source_ref: str,
        target_ref: str,
        properties: Mapping[str, object],
        *,
        transient: Mapping[str, object] | None = None,
    ) -> EdgeDraft:
        """Validate one canonical edge payload and return its detached Draft."""
        schema.require_edge(spec)
        schema.validate_operation(spec, CrudOperation.CREATE, properties)
        schema.validate_values(spec, properties)
        return cls(
            spec,
            source_ref,
            target_ref,
            properties,
            transient or {},
        )

    def output(self) -> Mapping[str, object]:
        """Return caller-owned Store input while endpoints remain plan refs."""
        caller_fields = {
            property_spec.name
            for property_spec in self.spec.properties
            if property_spec.owner is PropertyOwner.CALLER
        }
        return MappingProxyType(
            {
                name: value
                for name, value in self.properties.items()
                if name in caller_fields
            }
        )


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """Return one detached snapshot of a persisted canonical node.

    Unlike NodeDraft.ref, ``key`` is the exact persisted key generated or
    accepted by the Store according to the canonical NodeSpec. ``properties``
    may contain caller-, Scope-, and Store-owned values. The snapshot has no
    live connection to the backend: later writes do not update this object, and
    holding it does not lock or reserve the represented node.
    """

    spec: NodeSpec
    properties: Mapping[str, object]

    def __post_init__(self) -> None:
        """Detach backend values and require one scalar canonical key."""
        if type(self.spec) is not NodeSpec:
            raise TypeError("node record spec must be a NodeSpec")
        properties = freeze_mapping(
            self.properties,
            field_name="record values",
            key_field_name="record property",
        )
        require_trimmed_text(properties.get("key"), "node record key")
        object.__setattr__(self, "properties", properties)

    @property
    def key(self) -> str:
        """Return the exact persisted node key, never a plan-local Draft ref."""
        return require_trimmed_text(self.properties["key"], "node record key")


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """Return one detached snapshot of a persisted canonical edge.

    Persisted edges address endpoints by exact ``source_key`` and ``target_key``;
    the plan-local refs carried by EdgeDraft no longer appear. Together with the
    canonical EdgeSpec, those endpoint keys identify the logical edge exposed by
    GraphStore. Like NodeRecord, this object is an immutable snapshot rather than
    a live backend record or transaction lock.
    """

    spec: EdgeSpec
    source_key: str
    target_key: str
    properties: Mapping[str, object]

    def __post_init__(self) -> None:
        """Detach backend values and require exact scalar endpoint keys."""
        if type(self.spec) is not EdgeSpec:
            raise TypeError("edge record spec must be an EdgeSpec")
        object.__setattr__(
            self,
            "source_key",
            require_trimmed_text(self.source_key, "edge source key"),
        )
        object.__setattr__(
            self,
            "target_key",
            require_trimmed_text(self.target_key, "edge target key"),
        )
        object.__setattr__(
            self,
            "properties",
            freeze_mapping(
                self.properties,
                field_name="record values",
                key_field_name="record property",
            ),
        )


@dataclass(frozen=True, slots=True)
class SubgraphRecord:
    """Return one bounded, endpoint-closed collection of detached records."""

    nodes: tuple[NodeRecord, ...]
    edges: tuple[EdgeRecord, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        """Freeze order and reject duplicate or dangling logical records."""
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        if not all(type(node) is NodeRecord for node in nodes):
            raise TypeError("subgraph nodes must contain NodeRecord values")
        if not all(type(edge) is EdgeRecord for edge in edges):
            raise TypeError("subgraph edges must contain EdgeRecord values")
        if not isinstance(self.truncated, bool):
            raise TypeError("subgraph truncated must be boolean")
        for index, node in enumerate(nodes):
            if any(
                previous.spec is node.spec and previous.key == node.key
                for previous in nodes[:index]
            ):
                raise ValueError("subgraph contains a duplicate logical node")
        for index, edge in enumerate(edges):
            if any(
                previous.spec is edge.spec
                and previous.source_key == edge.source_key
                and previous.target_key == edge.target_key
                for previous in edges[:index]
            ):
                raise ValueError("subgraph contains a duplicate logical edge")
            source_present = any(
                node.spec is edge.spec.source and node.key == edge.source_key
                for node in nodes
            )
            target_present = any(
                node.spec is edge.spec.target and node.key == edge.target_key
                for node in nodes
            )
            if not source_present or not target_present:
                raise ValueError("subgraph edge endpoints must be present as nodes")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)


__all__ = [
    "EdgeDraft",
    "EdgeRecord",
    "GraphPropertyMap",
    "NodeDraft",
    "NodeRecord",
    "SubgraphRecord",
]
