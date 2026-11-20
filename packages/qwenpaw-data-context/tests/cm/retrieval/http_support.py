"""Provide one shared Store recording fake for thin API adapter tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from context_manager.api.app import create_api_app
from context_manager.api.dependencies import (
    require_context_authorization,
    require_context_runtime,
)
from context_manager.auth import GraphScopeAuthority, ScopeBinding
from context_manager.schema import GRAPH_SCHEMA, EdgeSpec, GraphSchema, NodeSpec
from context_manager.sql import RdbConnector
from context_manager.store import (
    EdgeRecord,
    GraphStore,
    NeighborDirection,
    NodeRecord,
)
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary


class RecordingStore(GraphStore):
    """Record API calls without reproducing Store validation or query logic."""

    backend = "neo4j"

    def __init__(self) -> None:
        """Create one canonical Schema-backed read fake."""
        self._access = GraphScopeAuthority()
        self.calls: list[tuple[str, NodeSpec | EdgeSpec, Mapping[str, object]]] = []
        self.list_result: Sequence[NodeRecord] = ()
        self.list_results: dict[NodeSpec, Sequence[NodeRecord]] = {}
        self.edge_results: dict[EdgeSpec, Sequence[EdgeRecord]] = {}
        self.get_result: NodeRecord | None = None
        self.neighbor_result: Sequence[tuple[NodeRecord, EdgeRecord]] = ()
        self.error: Exception | None = None

    @property
    def schema(self) -> GraphSchema:
        """Expose the same canonical GraphSchema used in production."""
        return GRAPH_SCHEMA

    def health(self):
        return {"ok": True}

    @property
    def access(self) -> GraphScopeAuthority:
        """Expose the CM Auth authority used to issue API test scopes."""
        return self._access

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
        """Record one list primitive invocation."""
        if self.error is not None:
            raise self.error
        self.calls.append(
            (
                "list_nodes",
                spec,
                {
                    "filters": None if filters is None else dict(filters),
                    "limit": limit,
                    "offset": offset,
                    "scope": scope,
                },
            )
        )
        if spec in self.list_results:
            values = tuple(self.list_results[spec])
            values = values[offset : offset + limit]
        else:
            values = self.list_result
        return tuple(
            record
            if fields is None
            else replace(
                record,
                properties={
                    name: record.properties.get(name) for name in ("key", *fields)
                },
            )
            for record in values
        )

    def list_edges(
        self,
        spec: EdgeSpec,
        *,
        filters: Mapping[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        scope: ScopeBinding,
    ) -> Sequence[EdgeRecord]:
        """Record one bounded edge-page invocation."""
        if self.error is not None:
            raise self.error
        self.calls.append(
            (
                "list_edges",
                spec,
                {
                    "filters": None if filters is None else dict(filters),
                    "limit": limit,
                    "offset": offset,
                    "scope": scope,
                },
            )
        )
        return tuple(self.edge_results.get(spec, ()))[offset : offset + limit]

    def get_nodes(self, spec, keys, *, fields=None, scope):
        if self.error is not None:
            raise self.error
        self.calls.append(("get_nodes", spec, {"keys": keys, "scope": scope}))
        return (self.get_result,) * len(keys)

    def expand_neighbors(
        self,
        node_spec: NodeSpec,
        key: str,
        *,
        direction: NeighborDirection = NeighborDirection.BOTH,
        limit: int = 100,
        offset: int = 0,
        scope: ScopeBinding,
    ) -> Sequence[tuple[NodeRecord, EdgeRecord]]:
        """Record one one-hop page invocation."""
        if self.error is not None:
            raise self.error
        self.calls.append(
            (
                "expand_neighbors",
                node_spec,
                {
                    "key": key,
                    "direction": direction,
                    "limit": limit,
                    "offset": offset,
                    "scope": scope,
                },
            )
        )
        return self.neighbor_result

    def patch_nodes(self, spec, patches, *, scope):
        if self.error is not None:
            raise self.error
        self.calls.append(("patch_nodes", spec, {"patches": patches, "scope": scope}))

    def patch_edges(self, spec, patches, *, scope):
        if self.error is not None:
            raise self.error
        self.calls.append(("patch_edges", spec, {"patches": patches, "scope": scope}))

    def atomic(self, work: object) -> object:
        """Execute the API's explicit patch-existing transaction."""
        return work(self)

    def execute_query(self, query, parameters=None, *, scope):
        raise AssertionError("unexpected execute_query in API recording fake")

    def create_nodes(self, spec, properties, *, scope):
        raise AssertionError("unexpected create_nodes in API recording fake")

    def delete_nodes(self, spec, keys, *, scope):
        raise AssertionError("unexpected delete_nodes in API recording fake")

    def get_edges(self, spec, endpoints, *, fields=None, scope):
        raise AssertionError("unexpected get_edges in API recording fake")

    def create_edges(self, spec, edges, *, fields=None, scope):
        raise AssertionError("unexpected create_edges in API recording fake")

    def delete_edges(self, spec, endpoints, *, scope):
        raise AssertionError("unexpected delete_edges in API recording fake")

    def search_fulltext(
        self, text, *, specs=None, filters=None, limit=20, fields=None, scope
    ):
        raise AssertionError("unexpected search_fulltext in API recording fake")

    def search_vector(
        self, embedding, *, specs=None, filters=None, limit=20, fields=None, scope
    ):
        raise AssertionError("unexpected search_vector in API recording fake")

    def close(self):
        pass


def build_client() -> tuple[TestClient, RecordingStore, ScopeBinding]:
    """Exercise real Runtime scope issuance with a recording Store."""
    store = RecordingStore()
    context = authorization()
    scope = store.access.issue_read_scope(context)
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
    )
    application = create_api_app()
    application.dependency_overrides[require_context_authorization] = lambda: context
    application.dependency_overrides[require_context_runtime] = lambda: runtime
    return TestClient(application), store, scope


__all__ = ["RecordingStore", "build_client"]
