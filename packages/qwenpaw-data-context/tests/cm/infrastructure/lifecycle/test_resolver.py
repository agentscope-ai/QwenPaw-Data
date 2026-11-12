"""Verify authenticated Workspace targets select isolated Context runtimes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

import pytest
from context_manager.db import initialize_database
from context_manager.runtime import (
    ContextRuntimeUnavailable,
    WorkspaceResolver,
    WorkspaceRuntimePool,
    create_workspace_runtime,
)
from context_manager.settings import Settings
from context_manager.settings.db import DbSettings
from qwenpaw_data_auth.database import get_db, init_schema
from neo4j import Record

from cm.infrastructure.execution.support import executor


class _Transaction:
    """Return deterministic health rows without a Neo4j connection."""

    def __init__(self, healthy: bool) -> None:
        """Configure whether the health query returns one row."""
        self._healthy = healthy

    def run(self, query: str, **_parameters: object) -> tuple[Record, ...]:
        """Answer only the runtime health query."""
        return (
            (Record({"ok": 1}),) if self._healthy and query == "RETURN 1 AS ok" else ()
        )


class _Session:
    """Execute one managed read against the in-memory transaction."""

    def __init__(self, healthy: bool) -> None:
        """Configure transaction health behavior."""
        self._healthy = healthy

    def execute_read(self, work: Any) -> object:
        """Invoke the supplied Neo4j work callback."""
        return work(_Transaction(self._healthy))

    def execute_write(self, work: Any) -> object:
        """Invoke one schema write callback."""
        return work(_Transaction(self._healthy))

    def __enter__(self) -> Self:
        """Enter the fake session."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Leave the fake session."""


class _Driver:
    """Capture database routing and terminal closure for one Workspace."""

    def __init__(self, *, healthy: bool = True) -> None:
        """Initialize empty session observations."""
        self._healthy = healthy
        self.session_options: list[dict[str, object]] = []
        self.close_count = 0

    def session(self, **options: object) -> _Session:
        """Open a fake session and retain its selected database."""
        self.session_options.append(dict(options))
        return _Session(self._healthy)

    def close(self) -> None:
        """Count terminal driver closure."""
        self.close_count += 1


class _DriverFactory:
    """Create and record a distinct fake driver for every runtime target."""

    def __init__(self, health_sequence: tuple[bool, ...] = ()) -> None:
        """Initialize empty connection and driver observations."""
        self._health_sequence = health_sequence
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.drivers: list[_Driver] = []

    def __call__(self, uri: str, **options: object) -> _Driver:
        """Create one driver for the supplied trusted Workspace reference."""
        index = len(self.drivers)
        healthy = (
            self._health_sequence[index] if index < len(self._health_sequence) else True
        )
        driver = _Driver(healthy=healthy)
        self.calls.append((uri, dict(options)))
        self.drivers.append(driver)
        return driver


def _seed_workspace(
    dsn: str,
    *,
    tenant_id: str,
    workspace_id: str,
    endpoint_ref: str,
    secret_ref: str,
    instance_status: str = "ready",
) -> None:
    """Insert one active tenant and Workspace into the real control-plane schema."""
    with get_db(dsn) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO tenants (id, name, neo4j_database)
            VALUES (?, ?, ?)
            """,
            (tenant_id, tenant_id, f"{tenant_id}-database"),
        )
        connection.execute(
            """
            INSERT INTO workspaces (
                id,
                tenant_id,
                name,
                endpoint_ref,
                secret_ref,
                instance_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                tenant_id,
                workspace_id,
                endpoint_ref,
                secret_ref,
                instance_status,
            ),
        )
        connection.commit()


@pytest.mark.asyncio
async def test_resolver_returns_isolated_pooled_workspace_runtimes(
    tmp_path: Path,
) -> None:
    """Each authenticated target receives its own trusted connection and cache key."""
    dsn = f"sqlite:///{tmp_path / 'control-plane.db'}"
    init_schema(dsn)
    initialize_database(DbSettings(url=dsn))
    first_secret = tmp_path / "workspace-one.json"
    first_secret.write_text(
        json.dumps(
            {
                "user": "neo4j-one",
                "password": "password-one",
                "database": "graph-one",
            }
        ),
        encoding="utf-8",
    )
    _seed_workspace(
        dsn,
        tenant_id="tenant-one",
        workspace_id="workspace-one",
        endpoint_ref="bolt://graph-one.internal:7687",
        secret_ref=f"file://{first_secret}",
    )
    _seed_workspace(
        dsn,
        tenant_id="tenant-two",
        workspace_id="workspace-two",
        endpoint_ref="env://WORKSPACE_TWO_ENDPOINT",
        secret_ref="env://WORKSPACE_TWO_SECRET",
    )
    factory = _DriverFactory()
    pool = WorkspaceRuntimePool(
        lambda connection: create_workspace_runtime(
            connection,
            driver_factory=factory,
            executor=executor,
            graph_read_executor=executor,
        )
    )
    resolver = WorkspaceResolver(
        pool,
        settings=Settings(
            environment={
                "WORKSPACE_TWO_ENDPOINT": "bolt://graph-two.internal:7687",
                "WORKSPACE_TWO_SECRET": json.dumps(
                    {"user": "neo4j-two", "password": "password-two"}
                ),
            }
        ),
    )

    assert factory.calls == []
    first = resolver.resolve("tenant-one", "workspace-one")
    first_sessions = tuple(factory.drivers[0].session_options)
    cached = resolver.resolve("tenant-one", "workspace-one")
    assert tuple(factory.drivers[0].session_options) == first_sessions
    second = resolver.resolve("tenant-two", "workspace-two")

    assert first is cached
    assert first is not second
    assert first.store.health() == {"ok": True}
    assert second.store.health() == {"ok": True}
    assert factory.calls == [
        (
            "bolt://graph-one.internal:7687",
            {
                "auth": ("neo4j-one", "password-one"),
                "notifications_min_severity": "OFF",
            },
        ),
        (
            "bolt://graph-two.internal:7687",
            {
                "auth": ("neo4j-two", "password-two"),
                "notifications_min_severity": "OFF",
            },
        ),
    ]
    assert factory.drivers[0].session_options
    assert all(
        options == {"database": "graph-one"}
        for options in factory.drivers[0].session_options
    )
    assert factory.drivers[1].session_options
    assert all(
        options == {"database": "tenant-two-database"}
        for options in factory.drivers[1].session_options
    )
    with pytest.raises(PermissionError, match="not registered"):
        resolver.resolve("tenant-one", "workspace-two")
    with pytest.raises(PermissionError, match="not registered"):
        resolver.require_workspace("tenant-one", "workspace-two")

    with get_db(dsn) as connection:
        connection.execute(
            "UPDATE workspaces SET status = 'inactive' WHERE id = ?", ("workspace-one",)
        )
        connection.commit()
    with pytest.raises(PermissionError, match="not active"):
        resolver.resolve("tenant-one", "workspace-one")
    with pytest.raises(PermissionError, match="not active"):
        resolver.require_workspace("tenant-one", "workspace-one")
    assert len(factory.calls) == 2
    with get_db(dsn) as connection:
        connection.execute(
            "UPDATE workspaces SET status = 'active' WHERE id = ?", ("workspace-one",)
        )
        connection.commit()
    await resolver.aclose()

    assert [driver.close_count for driver in factory.drivers] == [1, 1]
    with pytest.raises(ContextRuntimeUnavailable, match="pool is closed"):
        resolver.resolve("tenant-one", "workspace-one")
