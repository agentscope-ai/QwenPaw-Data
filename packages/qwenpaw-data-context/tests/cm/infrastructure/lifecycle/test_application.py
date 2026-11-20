"""Verify typed runtime composition, request binding, and application lifetime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest
from context_manager.api.app import (
    bind_context_runtime,
    mount_context_routes,
)
from context_manager.auth import (
    GraphScopeAuthority,
)
from context_manager.model import ModelService
from context_manager.runtime import ContextRuntime
from context_manager.schema import GRAPH_SCHEMA, Glossary
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore
from fastapi import FastAPI

from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary


class FakeExecutionRuntime:
    """Serve Metric rows through the Neo4j Store boundary."""

    def __init__(self) -> None:
        """Initialize one graph route and empty operation log."""
        self.target = GraphTarget("tenant-1", "workspace-1")
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def read(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
    ) -> Sequence[Mapping[str, object]]:
        """Return detached rows for the requested Store operation."""
        self.calls.append(("read", operation, dict(parameters)))
        if operation == "health":
            return ({"ok": 1},)
        if operation == "list_nodes" and "Metric" in query:
            return (
                {
                    "properties": {
                        "key": "metric-1",
                        "name": "Revenue",
                        "owner_id": "owner-1",
                        "workspace_id": "workspace-1",
                        "zone": "metadata",
                        "datasource_id": "warehouse",
                    }
                },
            )
        return ()

    def write(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
    ) -> Sequence[Mapping[str, object]]:
        """Reject writes in the read-only integration fixture."""
        raise AssertionError(f"unexpected write operation: {operation}")


class CloseCountingStore(Neo4jGraphStore):
    """Count runtime-owned Store close calls without a real driver."""

    def __init__(
        self,
        runtime: FakeExecutionRuntime,
        access: GraphScopeAuthority,
        glossary: Glossary,
    ) -> None:
        """Initialize one real Store adapter over the fake execution runtime."""
        super().__init__(runtime, GRAPH_SCHEMA, access, glossary)
        self.close_count = 0

    def close(self) -> None:
        """Count application resource closes."""
        self.close_count += 1


def _runtime(
    connector: RdbConnector | None = None,
    *,
    models: ModelService | None = None,
    model_service_factory: Callable[[str], ModelService] | None = None,
    model_config_dsn: str | None = None,
) -> tuple[ContextRuntime, FakeExecutionRuntime, CloseCountingStore]:
    """Compose one complete runtime without environment or network access."""
    execution = FakeExecutionRuntime()
    access = GraphScopeAuthority()
    glossary = make_glossary(GRAPH_SCHEMA)
    store = CloseCountingStore(
        execution,
        access,
        glossary,
    )
    runtime = bound_context_runtime(
        store,
        connector or RdbConnector(),
        glossary,
        model_config_dsn=model_config_dsn,
        models=models,
        model_service_factory=model_service_factory,
    )
    execution.calls.clear()
    return runtime, execution, store


def test_host_can_mount_routes_and_bind_runtime_without_lifespan_ownership() -> None:
    """A host can mount routes and bind a runtime while retaining close ownership."""
    runtime, _, store = _runtime()
    host = FastAPI()

    mount_context_routes(host)
    bind_context_runtime(host, runtime)

    assert host.state.context_runtime is runtime
    assert "/api/v1/graph/list_nodes" in host.openapi()["paths"]
    with pytest.raises(RuntimeError, match="already mounted"):
        mount_context_routes(host)
    with pytest.raises(RuntimeError, match="already bound"):
        bind_context_runtime(host, runtime)
    assert runtime.closed is False
    runtime.close()
    runtime.close()
    assert store.close_count == 1
