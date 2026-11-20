"""Define the small database-neutral graph persistence contract."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import wraps
from typing import TypeVar, cast

from ..auth import GLOBAL_OWNER_ID, AccessMode, GraphScopeAuthority, ScopeBinding
from ..schema import (
    CrudOperation,
    EdgeSpec,
    Glossary,
    GraphSchema,
    KeySpec,
    NodeSpec,
    PropertyOwner,
    ValueKind,
)
from ..utils.validation import require_nonempty_text, require_trimmed_text
from .models import EdgeRecord, NodeRecord, StoreError
from .runtime import GraphRuntime

_T = TypeVar("_T")
_PrimitiveT = TypeVar("_PrimitiveT", bound=Callable[..., object])
_PRIMITIVE_MARKER = "__store_primitive__"


def primitive(method: _PrimitiveT) -> _PrimitiveT:
    """Mark primitives and retain failed writes on their enclosing transaction."""
    if method.__name__ in {
        "create_nodes",
        "patch_nodes",
        "delete_nodes",
        "create_edges",
        "patch_edges",
        "delete_edges",
    }:

        @wraps(method)
        def write(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                self._runtime.fail_transaction()
                raise

        setattr(write, _PRIMITIVE_MARKER, True)
        return cast(_PrimitiveT, write)
    setattr(method, _PRIMITIVE_MARKER, True)
    return method


def is_primitive(method: object) -> bool:
    """Return whether a GraphStore method declares a primitive."""
    return getattr(method, _PRIMITIVE_MARKER, False) is True


class NeighborDirection(StrEnum):
    """Select the canonical edge orientation used by one-hop expansion."""

    BOTH = "both"
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class GraphStore(ABC):
    """Persist only canonical NodeSpec and EdgeSpec objects.

    Batch CRUD uses one spec and scope per call. Get/create results follow input
    order; get retains None for missing or invisible targets. Patch/delete return
    None on success and raise on failure; unmatched targets are no-ops. Empty
    batches use the same return contract without database work. Writes reject
    repeated targets and commit as one unit; inside atomic(), they participate
    in the enclosing transaction instead.

    Projection fields default to None (all properties). An empty sequence returns
    identity only; explicit fields return identity plus those properties, with
    None for absent values. Node keys and edge endpoints are always retained.
    Fields never change filtering, ordering or Scope enforcement. Search and
    neighbor projections may select fields from any participating canonical type.
    """

    def _bind(
        self,
        runtime: GraphRuntime,
        schema: GraphSchema,
        access: GraphScopeAuthority,
        glossary: Glossary,
    ) -> None:
        """Bind the exact logical declarations and authorization authority."""
        if type(schema) is not GraphSchema:
            raise TypeError("schema must be a GraphSchema")
        if not isinstance(access, GraphScopeAuthority):
            raise TypeError("access must be a GraphScopeAuthority")
        if type(glossary) is not Glossary:
            raise TypeError("glossary must be a Glossary")
        if glossary.schema is not schema:
            raise ValueError("Glossary must govern the Store schema")
        self._runtime = runtime
        self._schema = schema
        self._access = access
        self._glossary = glossary
        self._vector_dimensions: dict[NodeSpec, int] = {}

    @property
    @abstractmethod
    def backend(self) -> str:
        """Identify the storage backend without exposing the physical route."""

    @property
    def schema(self) -> GraphSchema:
        """Return the exact canonical schema accepted by this Store."""
        return self._schema

    @property
    def access(self) -> GraphScopeAuthority:
        """Return the exact CM Auth authority accepted by this Store."""
        return self._access

    @property
    def fulltext_indexes(self) -> Mapping[str, str]:
        """Map callable full-text indexes to public node types, when supported."""
        return {}

    @property
    def embedding_dimensions(self) -> frozenset[int]:
        """Return the vector dimensions required by this Store's schema."""
        return frozenset(self._vector_dimensions.values())

    @staticmethod
    def _projection_fields(
        specs: Sequence[NodeSpec | EdgeSpec],
        fields: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        """Validate selected properties and retain canonical node identity."""
        if fields is None:
            return None
        if isinstance(fields, (str, bytes)):
            raise TypeError("fields must be a sequence of property names")
        selected = dict.fromkeys(fields)
        allowed = {prop.name for spec in specs for prop in spec.properties}
        if unknown := selected.keys() - allowed:
            raise ValueError(f"unknown projection fields: {sorted(unknown)}")
        if all(isinstance(spec, NodeSpec) for spec in specs):
            selected = dict.fromkeys(("key", *selected))
        return tuple(selected)

    def _prepare_read(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> None:
        """Validate canonical schema ownership and read authorization once."""
        if isinstance(spec, NodeSpec):
            self._schema.require_node(spec)
        elif isinstance(spec, EdgeSpec):
            self._schema.require_edge(spec)
        else:
            raise TypeError("spec must be a NodeSpec or EdgeSpec")
        self._schema.validate_operation(spec, CrudOperation.READ)
        self._access.require(scope, AccessMode.READ)
        self._require_target(scope)
        expected_zone = (
            spec.zone.value if isinstance(spec, NodeSpec) else spec.source.zone.value
        )
        if scope.zone is not None and scope.zone != expected_zone:
            raise PermissionError("scope zone does not own this schema object")

    def _prepare_write(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
        operation: CrudOperation,
    ) -> None:
        """Validate canonical ownership and authorization once per write batch."""
        if isinstance(spec, NodeSpec):
            self._schema.require_node(spec)
            expected_zone = spec.zone.value
        elif isinstance(spec, EdgeSpec):
            self._schema.require_edge(spec)
            expected_zone = spec.source.zone.value
        else:
            raise TypeError("spec must be a NodeSpec or EdgeSpec")
        if scope.zone is not None and scope.zone != expected_zone:
            raise PermissionError("scope zone does not own this schema object")
        self._access.require(scope, AccessMode.WRITE)
        self._require_target(scope)
        if scope.write_owner_id is None:
            raise PermissionError("data writes require a single CM Auth-bound owner")
        self._schema.validate_operation(spec, operation)

    def _tag_labels(
        self,
        spec: NodeSpec,
        values: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Validate managed values and resolve canonical glossary labels."""
        labels: set[str] = set()
        for property_name in self._glossary.managed_properties(spec):
            if property_name not in values:
                continue
            value = values[property_name]
            if not isinstance(value, str):
                raise TypeError(f"{spec.name}.{property_name} must be text")
            term = self._glossary.resolve_term(
                spec,
                property_name,
                value,
            )
            labels.update(term.graph_labels)
        return tuple(sorted(labels))

    def _node_batch_rows(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        unique: bool = False,
    ) -> list[dict[str, object]]:
        """Validate exact keys and attach positions for unordered query results."""
        if isinstance(keys, (str, bytes)):
            raise TypeError("node keys must be a sequence of keys")
        rows = []
        seen = set()
        for index, key in enumerate(keys):
            key = require_trimmed_text(key, f"{spec.name} key")
            self._schema.validate_values(spec, {"key": key})
            if unique and key in seen:
                raise ValueError("node batch contains a repeated write target")
            seen.add(key)
            rows.append({"index": index, "key": key})
        return rows

    def _edge_batch_rows(
        self,
        spec: EdgeSpec,
        endpoints: Sequence[tuple[str, str]],
        *,
        unique: bool = False,
    ) -> list[dict[str, object]]:
        """Validate endpoint pairs and attach their input positions."""
        rows = []
        seen = set()
        for index, (source, target) in enumerate(endpoints):
            source = require_trimmed_text(source, "source key")
            target = require_trimmed_text(target, "target key")
            self._schema.validate_values(spec.source, {"key": source})
            self._schema.validate_values(spec.target, {"key": target})
            if unique and (source, target) in seen:
                raise ValueError("edge batch contains a repeated write target")
            seen.add((source, target))
            rows.append({"index": index, "source_key": source, "target_key": target})
        return rows

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        """Validate one bounded page."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be an integer from 1 to 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return limit, offset

    def _search_specs(
        self,
        specs: Sequence[NodeSpec] | None,
        kind: str,
        filters: Mapping[str, object] | None,
        scope: ScopeBinding,
        filters_by_type: Mapping[str, Mapping[str, object]] | None = None,
    ) -> dict[NodeSpec, dict[str, object]]:
        """Select searchable types and normalize filters once before backend reads."""
        self._access.require(scope, AccessMode.READ)
        self._require_target(scope)
        if filters_by_type:
            if specs is None:
                raise ValueError("filters_by_type requires explicit specs")
            if unknown := filters_by_type.keys() - {spec.name for spec in specs}:
                raise ValueError(
                    f"filters_by_type contains unselected types: {sorted(unknown)}"
                )
        if specs is not None:
            selected = {}
            for spec in dict.fromkeys(specs):
                self._schema.require_node(spec)
                if CrudOperation.READ not in spec.operations:
                    raise PermissionError("read is denied for schema owner")
                if scope.zone is not None and scope.zone != spec.zone.value:
                    raise PermissionError("scope zone does not own this schema object")
                if not (
                    spec.fulltext_fields
                    if kind == "fulltext"
                    else spec.embedding_fields
                ):
                    raise ValueError(f"{spec.name} does not support {kind} search")
                combined = dict(filters or {})
                for name, value in (filters_by_type or {}).get(spec.name, {}).items():
                    if name in combined and combined[name] != value:
                        raise ValueError(
                            f"conflicting search filter: {spec.name}.{name}"
                        )
                    combined[name] = value
                selected[spec] = self._filters(spec, combined)
            return selected
        specs = tuple(
            item
            for item in self._schema.nodes
            if CrudOperation.READ in item.operations
            and (scope.zone is None or item.zone.value == scope.zone)
            and (item.fulltext_fields if kind == "fulltext" else item.embedding_fields)
        )
        fields = set(filters or {})
        known = {prop.name for item in specs for prop in item.properties}
        if fields - known:
            raise ValueError(f"unknown properties: {sorted(fields - known)}")
        specs = tuple(
            item for item in specs if fields <= {prop.name for prop in item.properties}
        )
        return {item: self._filters(item, filters) for item in specs}

    def _require_target(self, scope: ScopeBinding) -> None:
        """Reject a valid capability issued for another runtime route."""
        if (
            scope.tenant_id != self._runtime.target.tenant_id
            or scope.workspace_id != self._runtime.target.workspace_id
        ):
            raise PermissionError("scope does not match the graph target")

    @staticmethod
    def _logical_zone(spec: NodeSpec | EdgeSpec) -> str:
        """Resolve the canonical zone of one node or edge owner."""
        return spec.zone.value if isinstance(spec, NodeSpec) else spec.source.zone.value

    @staticmethod
    def _scope_properties(spec: NodeSpec | EdgeSpec) -> tuple[str, ...]:
        """Return scope-owned properties declared by one schema object."""
        return tuple(
            prop.name for prop in spec.properties if prop.owner is PropertyOwner.SCOPE
        )

    def _scope_inputs(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> dict[str, object]:
        """Supply values for every declared scope property, including optional nulls."""
        supplied = {
            "workspace_id": scope.workspace_id,
            "owner_id": scope.write_owner_id,
            "zone": self._logical_zone(spec),
            "datasource_id": scope.datasource_id,
        }
        values = {}
        for name in self._scope_properties(spec):
            if name not in supplied:
                raise ValueError(f"Store cannot supply scoped property {name!r}")
            values[name] = supplied[name]
        return values

    def _scope_values(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> dict[str, object]:
        """Supply non-null scope-owned fields for a validated write."""
        return {
            name: value
            for name, value in self._scope_inputs(spec, scope).items()
            if value is not None
        }

    def _filters(
        self,
        spec: NodeSpec | EdgeSpec,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """Copy and validate equality filters before any backend reads."""
        values = dict(filters or {})
        self._validate_values(spec, values)
        return values

    def _validate_values(
        self,
        spec: NodeSpec | EdgeSpec,
        values: dict[str, object],
        *,
        complete: bool = False,
    ) -> None:
        """Validate owned input maps and normalize only logical temporal fields."""
        self._schema.validate_values(spec, values, complete=complete)
        for name, value in values.items():
            if value is None:
                continue
            kind = spec.property(name).kind
            if kind is ValueKind.DATETIME:
                if isinstance(value, str):
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        value = date.fromisoformat(value)
                    else:
                        if re.search(r"[.,]\d{7,}", value):
                            raise ValueError(f"{name} exceeds microsecond precision")
                        value = datetime.fromisoformat(value)
                if isinstance(value, datetime):
                    if value.utcoffset() is None:
                        raise ValueError(f"{name} requires a timezone")
                    values[name] = (
                        value.astimezone(UTC)
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z")
                    )
                else:
                    values[name] = value.isoformat()
            elif kind is ValueKind.VECTOR:
                if not value or not any(value):
                    raise ValueError(f"{name} must be a nonempty, nonzero vector")
                dimension = self._vector_dimensions.get(spec)
                if dimension is not None and len(value) != dimension:
                    raise ValueError(f"{name} dimensions do not match the Store schema")
                if not math.isfinite(math.hypot(*value)):
                    raise ValueError(f"{name} norm must be finite")

    def _node_create_rows(
        self,
        spec: NodeSpec,
        properties: Sequence[Mapping[str, object]],
        scope: ScopeBinding,
    ) -> list[dict[str, object]]:
        """Validate all node creates and derive their canonical keys and glossary labels."""
        self._prepare_write(spec, scope, CrudOperation.CREATE)
        scope_values = self._scope_values(spec, scope)
        key_spec = spec.property("key")
        if not isinstance(key_spec, KeySpec):
            raise TypeError(f"{spec.name} key must be declared by KeySpec")
        items = []
        keys = set()
        for index, values in enumerate(properties):
            if not isinstance(values, Mapping):
                raise TypeError("write values must be a mapping")
            self._schema.validate_operation(spec, CrudOperation.CREATE, values.keys())
            persisted = {**values, **scope_values}
            key = require_trimmed_text(key_spec.generate(persisted), "node key")
            persisted[key_spec.name] = key
            self._validate_values(spec, persisted, complete=True)
            labels = self._tag_labels(spec, persisted)
            if key in keys:
                raise ValueError("node batch contains a repeated write target")
            keys.add(key)
            items.append(
                {
                    "index": index,
                    "key": key,
                    "properties": {
                        name: value
                        for name, value in persisted.items()
                        if value is not None
                    },
                    "labels": labels,
                }
            )
        return items

    def _edge_create_rows(
        self,
        spec: EdgeSpec,
        edges: Sequence[tuple[str, str, Mapping[str, object]]],
        scope: ScopeBinding,
    ) -> list[dict[str, object]]:
        """Validate all edge creates and supply their canonical scope fields."""
        self._prepare_write(spec, scope, CrudOperation.CREATE)
        items = self._edge_batch_rows(
            spec,
            tuple((source, target) for source, target, _ in edges),
            unique=True,
        )
        scope_values = self._scope_values(spec, scope)
        for item, (_, _, values) in zip(items, edges, strict=True):
            if not isinstance(values, Mapping):
                raise TypeError("write values must be a mapping")
            self._schema.validate_operation(spec, CrudOperation.CREATE, values.keys())
            persisted = {**values, **scope_values}
            self._validate_values(spec, persisted, complete=True)
            item["properties"] = {
                name: value for name, value in persisted.items() if value is not None
            }
        return items

    def _patch_values(
        self,
        spec: NodeSpec | EdgeSpec,
        changes: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate one nonempty patch before backend mutation starts."""
        if not changes:
            owner = "node" if isinstance(spec, NodeSpec) else "edge"
            raise ValueError(f"{owner} changes cannot be empty")
        if not isinstance(changes, Mapping):
            raise TypeError("write values must be a mapping")
        self._schema.validate_operation(spec, CrudOperation.UPDATE, changes.keys())
        values = dict(changes)
        self._validate_values(spec, values)
        return values

    def _neighbor_specs(
        self,
        node_spec: NodeSpec,
        direction: NeighborDirection,
    ) -> tuple[EdgeSpec, ...]:
        """Select readable canonical edge triples in stable schema order."""
        readable = tuple(
            edge
            for edge in self._schema.edges
            if CrudOperation.READ in edge.operations
            and CrudOperation.READ in edge.source.operations
            and CrudOperation.READ in edge.target.operations
        )
        return tuple(
            sorted(
                (
                    edge
                    for edge in readable
                    if (
                        direction is NeighborDirection.BOTH
                        and (edge.source is node_spec or edge.target is node_spec)
                    )
                    or (
                        direction is NeighborDirection.OUTGOING
                        and edge.source is node_spec
                    )
                    or (
                        direction is NeighborDirection.INCOMING
                        and edge.target is node_spec
                    )
                ),
                key=lambda edge: edge.triple,
            )
        )

    def initialize_user_space(self, scope: ScopeBinding) -> None:
        """Authorize sync allocation; shared-graph backends need no user DDL."""
        self._access.require(scope, AccessMode.WRITE)
        self._require_target(scope)
        if scope.write_owner_id in {None, GLOBAL_OWNER_ID}:
            raise PermissionError("user space initialization requires a personal owner")

    @abstractmethod
    def health(self) -> Mapping[str, object]:
        """Return a narrow backend-neutral liveness summary."""
        ...

    @primitive
    def execute_query(
        self,
        query: str,
        parameters: Mapping[str, object] | None = None,
        *,
        scope: ScopeBinding,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """Execute an application-owned native read without rewriting its results.

        Holo accepts complete AGE SQL; Neo4j accepts Cypher. Callers own query
        admission and must restrict every referenced graph, node and edge to
        the issued Scope. This primitive is not an untrusted query console.
        """
        self._access.require(scope, AccessMode.READ)
        self._require_target(scope)
        if max_rows is not None and (type(max_rows) is not int or max_rows < 1):
            raise ValueError("max_rows must be a positive integer")
        return self._runtime.read(
            require_nonempty_text(query, "query"),
            dict(parameters or {}),
            operation="execute_query",
            max_rows=max_rows,
        )

    @primitive
    @abstractmethod
    def get_nodes(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[NodeRecord | None, ...]:
        """Read exact keys, retaining missing entries and repeated input keys."""
        ...

    @primitive
    @abstractmethod
    def create_nodes(
        self,
        spec: NodeSpec,
        properties: Sequence[Mapping[str, object]],
        *,
        scope: ScopeBinding,
    ) -> tuple[str, ...]:
        """Create all nodes and return persisted keys in input order."""
        ...

    @primitive
    @abstractmethod
    def patch_nodes(
        self,
        spec: NodeSpec,
        patches: Sequence[tuple[str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Apply (key, changes) pairs; each nonempty patch may change different fields."""
        ...

    @primitive
    @abstractmethod
    def delete_nodes(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Delete exact keys without implicitly deleting their incident edges."""
        ...

    @primitive
    @abstractmethod
    def get_edges(
        self,
        spec: EdgeSpec,
        endpoints: Sequence[tuple[str, str]],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[EdgeRecord | None, ...]:
        """Read (source_key, target_key) pairs, retaining missing and repeated entries."""
        ...

    @primitive
    @abstractmethod
    def create_edges(
        self,
        spec: EdgeSpec,
        edges: Sequence[tuple[str, str, Mapping[str, object]]],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[EdgeRecord, ...]:
        """Insert (source_key, target_key, properties) triples; every endpoint must exist.

        Creation does not update an existing edge. Callers explicitly choose
        patch_edges for changes and own any business deduplication policy.
        """
        ...

    @primitive
    @abstractmethod
    def patch_edges(
        self,
        spec: EdgeSpec,
        patches: Sequence[tuple[str, str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Apply (source_key, target_key, changes) triples with nonempty changes."""
        ...

    @primitive
    @abstractmethod
    def delete_edges(
        self,
        spec: EdgeSpec,
        endpoints: Sequence[tuple[str, str]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Delete exact endpoint pairs; successful execution returns no value."""
        ...

    @primitive
    @abstractmethod
    def list_nodes(
        self,
        spec: NodeSpec,
        *,
        filters: Mapping[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Read one bounded node page using equality filters."""
        ...

    @primitive
    @abstractmethod
    def search_fulltext(
        self,
        text: str,
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        filters_by_type: Mapping[str, Mapping[str, object]] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Search selected or all searchable types, ordered by raw score descending."""
        ...

    @primitive
    @abstractmethod
    def search_vector(
        self,
        embedding: Sequence[float],
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        filters_by_type: Mapping[str, Mapping[str, object]] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Search selected or all searchable types, ordered by vector similarity."""
        ...

    @primitive
    @abstractmethod
    def expand_neighbors(
        self,
        node_spec: NodeSpec,
        key: str,
        *,
        direction: NeighborDirection = NeighborDirection.BOTH,
        limit: int = 100,
        offset: int = 0,
        node_fields: Sequence[str] | None = None,
        edge_fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[tuple[NodeRecord, EdgeRecord]]:
        """Return one stable page of neighbors and their directed connecting edges."""
        ...

    @primitive
    @abstractmethod
    def list_edges(
        self,
        spec: EdgeSpec,
        *,
        filters: Mapping[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[EdgeRecord]:
        """Read one bounded edge page using equality filters."""
        ...

    @abstractmethod
    def atomic(self, work: Callable[[GraphStore], _T]) -> _T:
        """Execute a finite callback in one backend transaction."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release Store-owned resources idempotently."""
        ...


__all__ = ["GraphStore", "NeighborDirection", "StoreError", "primitive", "is_primitive"]
