"""Provide a transaction-aware in-memory GraphStore for service tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TypeVar

from context_manager.auth import AccessMode, GraphScopeAuthority, ScopeBinding
from context_manager.schema import (
    GRAPH_SCHEMA,
    EdgeSpec,
    GraphSchema,
    KeySpec,
    NodeSpec,
    PropertyOwner,
)
from context_manager.store import (
    EdgeRecord,
    GraphStore,
    NeighborDirection,
    NodeRecord,
)
from context_manager.utils.validation import require_trimmed_text

_T = TypeVar("_T")


class MemoryGraphStore(GraphStore):
    """Implement the finite Store protocol with rollback-capable dictionaries."""

    backend = "neo4j"

    def __init__(self) -> None:
        """Initialize one canonical graph and operation counters."""
        self._access = GraphScopeAuthority()
        self._vector_dimensions = {
            node: 1024 for node in GRAPH_SCHEMA.nodes if node.embedding_fields
        }
        self.nodes: dict[tuple[NodeSpec, str], dict[str, object]] = {}
        self.edges: dict[
            tuple[EdgeSpec, str, str],
            dict[str, object],
        ] = {}
        self.transaction_bound = False
        self.closed = False
        self.fail_edge_write = False

    @property
    def schema(self) -> GraphSchema:
        """Return the canonical test schema."""
        return GRAPH_SCHEMA

    @property
    def access(self) -> GraphScopeAuthority:
        """Return the exact capability issuer used by operations."""
        return self._access

    def health(self) -> Mapping[str, object]:
        """Return an in-memory liveness response."""
        return {"ok": not self.closed}

    def initialize_user_space(self, scope: ScopeBinding) -> None:
        """The in-memory backend has no physical spaces to allocate."""
        self._access.require(scope, AccessMode.WRITE)

    def execute_query(self, query, parameters=None, *, scope):
        raise NotImplementedError("Use Neo4j for native query integration tests")

    def get_nodes(self, spec, keys, *, fields=None, scope):
        results = []
        for key in keys:
            self._read(spec, scope)
            values = self.nodes.get((spec, require_trimmed_text(key, "node key")))
            if values is None or not self._visible(values, scope):
                results.append(None)
                continue
            results.append(NodeRecord(spec, values))
        return tuple(
            self._project(record, fields) if record is not None else None
            for record in results
        )

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
        """Return one deterministic filtered page."""
        self._read(spec, scope)
        filters = dict(filters or {})
        rows = [
            NodeRecord(spec, values)
            for (owner, _), values in self.nodes.items()
            if owner is spec
            and all(values.get(name) == value for name, value in filters.items())
            and self._visible(values, scope)
        ]
        rows.sort(key=lambda record: record.key)
        return tuple(
            self._project(record, fields) for record in rows[offset : offset + limit]
        )

    def search_fulltext(
        self,
        text: str,
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Match substrings with equal relevance for HTTP tests."""
        specs = (
            specs
            if specs is not None
            else tuple(
                item
                for item in self.schema.nodes
                if item.fulltext_fields
                and (scope.zone is None or scope.zone == item.zone.value)
                and set(filters or {}) <= {prop.name for prop in item.properties}
            )
        )
        query = text.casefold()
        ranked = []
        for item in specs:
            rows = self.list_nodes(
                item, filters=filters, limit=len(self.nodes), scope=scope
            )
            matched = (
                row
                for row in rows
                if query
                in " ".join(
                    str(row.properties.get(field) or "")
                    for field in item.fulltext_fields
                ).casefold()
            )
            ranked.extend(matched)
        ranked.sort(key=lambda row: (row.spec.name, row.key))
        return tuple(self._project(row, fields) for row in ranked[:limit])

    def search_vector(
        self,
        embedding: Sequence[float],
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Reject indexed search because Knowledge tests use their own recall fake."""
        raise AssertionError("knowledge service tests must not use Store search")

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
        """Read one page of visible neighbors from the seeded edges."""
        direction = NeighborDirection(direction)
        if self.get_nodes(node_spec, (key,), scope=scope)[0] is None:
            return ()
        neighbors = []
        for (edge, source, target), values in self.edges.items():
            if not self._visible(values, scope):
                continue
            if (
                direction is not NeighborDirection.INCOMING
                and edge.source is node_spec
                and source == key
            ):
                spec, neighbor_key = edge.target, target
            elif (
                direction is not NeighborDirection.OUTGOING
                and edge.target is node_spec
                and target == key
            ):
                spec, neighbor_key = edge.source, source
            else:
                continue
            node = self.get_nodes(
                spec, (neighbor_key,), fields=node_fields, scope=scope
            )[0]
            if node is not None:
                neighbors.append(
                    (
                        node,
                        self._project(
                            EdgeRecord(edge, source, target, values), edge_fields
                        ),
                    )
                )
        neighbors.sort(
            key=lambda item: (item[0].spec.name, item[0].key, item[1].spec.relationship)
        )
        return tuple(neighbors[offset : offset + limit])

    @staticmethod
    def _project(record, fields):
        if fields is None:
            return record
        names = ("key", *fields) if isinstance(record, NodeRecord) else fields
        return replace(
            record, properties={name: record.properties.get(name) for name in names}
        )

    def create_nodes(self, spec, properties, *, scope):
        results = []
        for values_in in properties:
            self._write(spec, scope)
            values = {**dict(values_in), **self._scope_values(spec, scope)}
            key_spec = next(
                (prop for prop in spec.properties if isinstance(prop, KeySpec)), None
            )
            if key_spec is not None and key_spec.name not in values:
                values[key_spec.name] = key_spec.generate(values)
            self.schema.validate_values(spec, values, complete=True)
            key = (spec, require_trimmed_text(values["key"], "node key"))
            if key in self.nodes:
                raise RuntimeError("duplicate node")
            self.nodes[key] = values
            results.append(values["key"])
        return tuple(results)

    def patch_nodes(self, spec, patches, *, scope):
        for key, changes in patches:
            self._write(spec, scope)
            node_key = (spec, require_trimmed_text(key, "node key"))
            if node_key not in self.nodes or not self._visible(
                self.nodes[node_key], scope
            ):
                continue
            self.nodes[node_key].update(changes)

    def delete_nodes(self, spec, keys, *, scope):
        for key in keys:
            self._write(spec, scope)
            node_key = (spec, require_trimmed_text(key, "node key"))
            current = self.nodes.get(node_key)
            if current is not None and self._visible(current, scope):
                self.nodes.pop(node_key)

    def get_edges(self, spec, endpoints, *, fields=None, scope):
        results = []
        for source_key, target_key in endpoints:
            self._read(spec, scope)
            key = self._edge_key(spec, source_key, target_key)
            values = self.edges.get(key)
            if values is None or not self._visible(values, scope):
                results.append(None)
                continue
            results.append(EdgeRecord(spec, source_key, target_key, values))
        return tuple(
            self._project(record, fields) if record is not None else None
            for record in results
        )

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
        """Return one deterministic exact-edge page."""
        self._read(spec, scope)
        filters = dict(filters or {})
        rows = [
            EdgeRecord(owner, source, target, values)
            for (owner, source, target), values in self.edges.items()
            if owner is spec
            and all(values.get(name) == value for name, value in filters.items())
            and self._visible(values, scope)
        ]
        return tuple(
            self._project(record, fields) for record in rows[offset : offset + limit]
        )

    def create_edges(self, spec, edges, *, fields=None, scope):
        results = []
        for source_key, target_key, properties in edges:
            self._write(spec, scope)
            if self.fail_edge_write:
                raise RuntimeError("simulated edge write failure")
            source = self.nodes.get(
                (spec.source, require_trimmed_text(source_key, "edge source key"))
            )
            target = self.nodes.get(
                (spec.target, require_trimmed_text(target_key, "edge target key"))
            )
            if source is None or not self._visible_endpoint(source, scope):
                raise RuntimeError("source endpoint is missing")
            if target is None or not self._visible_endpoint(target, scope):
                raise RuntimeError("target endpoint is missing")
            values = {**dict(properties), **self._scope_values(spec, scope)}
            key = self._edge_key(spec, source_key, target_key)
            if key in self.edges:
                raise RuntimeError("duplicate edge")
            self.edges[key] = values
            results.append(EdgeRecord(spec, source_key, target_key, values))
        return tuple(
            self._project(record, fields) if record is not None else None
            for record in results
        )

    def patch_edges(self, spec, patches, *, scope):
        for source_key, target_key, changes in patches:
            self._write(spec, scope)
            key = self._edge_key(spec, source_key, target_key)
            if key not in self.edges or not self._visible(self.edges[key], scope):
                continue
            self.edges[key].update(changes)

    def delete_edges(self, spec, endpoints, *, scope):
        for source_key, target_key in endpoints:
            self._write(spec, scope)
            key = self._edge_key(spec, source_key, target_key)
            current = self.edges.get(key)
            if current is not None and self._visible(current, scope):
                self.edges.pop(key)

    def atomic(self, work: Callable[[MemoryGraphStore], _T]) -> _T:
        """Rollback dictionaries when a transaction callback fails."""
        if self.transaction_bound:
            return work(self)
        nodes = {key: dict(values) for key, values in self.nodes.items()}
        edges = {key: dict(values) for key, values in self.edges.items()}
        self.transaction_bound = True
        try:
            return work(self)
        except Exception:
            self.nodes = nodes
            self.edges = edges
            raise
        finally:
            self.transaction_bound = False

    def close(self) -> None:
        """Mark this fake closed."""
        self.closed = True

    def _read(self, owner: NodeSpec | EdgeSpec, scope: ScopeBinding) -> None:
        """Validate canonical ownership and read access."""
        if isinstance(owner, NodeSpec):
            self.schema.require_node(owner)
        else:
            self.schema.require_edge(owner)
        self.access.require(scope, AccessMode.READ)

    def _write(self, owner: NodeSpec | EdgeSpec, scope: ScopeBinding) -> None:
        """Validate canonical ownership and write access."""
        if isinstance(owner, NodeSpec):
            self.schema.require_node(owner)
        else:
            self.schema.require_edge(owner)
        self.access.require(scope, AccessMode.WRITE)

    def _edge_key(
        self,
        spec: EdgeSpec,
        source: str,
        target: str,
    ) -> tuple[EdgeSpec, str, str]:
        """Freeze one endpoint-qualified edge key."""
        return (
            spec,
            require_trimmed_text(source, "edge source key"),
            require_trimmed_text(target, "edge target key"),
        )

    @staticmethod
    def _scope_values(
        owner: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> dict[str, object]:
        """Inject only scope-owned properties declared by one schema owner."""
        values = {
            "owner_id": scope.write_owner_id,
            "workspace_id": scope.workspace_id,
            "zone": owner.zone.value
            if isinstance(owner, NodeSpec)
            else owner.source.zone.value,
            "datasource_id": scope.datasource_id,
        }
        names = {
            prop.name for prop in owner.properties if prop.owner is PropertyOwner.SCOPE
        }
        return {
            name: value
            for name, value in values.items()
            if name in names and value is not None
        }

    @staticmethod
    def _visible(values: Mapping[str, object], scope: ScopeBinding) -> bool:
        """Apply workspace, owner, zone, and datasource visibility for reads."""
        if values.get("workspace_id") != scope.workspace_id:
            return False
        if scope.owner_ids and values.get("owner_id") not in scope.owner_ids:
            return False
        if (
            scope.datasource_id is not None
            and values.get("datasource_id") != scope.datasource_id
        ):
            return False
        return scope.zone is None or values.get("zone") == scope.zone

    @staticmethod
    def _visible_endpoint(
        values: Mapping[str, object], scope: ScopeBinding
    ) -> bool:
        """Check edge endpoints without forging a zone-widened capability."""
        if values.get("workspace_id") != scope.workspace_id:
            return False
        if scope.owner_ids and values.get("owner_id") not in scope.owner_ids:
            return False
        return (
            scope.datasource_id is None
            or values.get("datasource_id") == scope.datasource_id
        )


__all__ = ["MemoryGraphStore"]
