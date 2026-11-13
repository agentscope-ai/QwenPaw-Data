"""Verify process startup, qwenpaw-data-auth binding, and request Scope issuance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import pytest
from context_manager.api.app import create_service_app
from context_manager.auth import get_authorization
from context_manager.db import (
    CMDatabase,
    close_database,
    get_database,
    initialize_database,
)
from context_manager.runtime import (
    ContextRuntime,
    Neo4jConnectionConfig,
)
from context_manager.settings import get_settings
from context_manager.settings.db import DbSettings
from qwenpaw_data_auth.authorization import AuthorizationContext
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, init_schema
from qwenpaw_data_auth.token import TokenIssuer
from fastapi import Request
from fastapi.testclient import TestClient
from neo4j import Record

from cm.infrastructure.lifecycle.support import create_external_tables


class FakeTransaction:
    """Capture prepared Cypher and the request-local Auth result."""

    def __init__(self, driver: FakeDriver) -> None:
        """Bind the owning fake driver."""
        self._driver = driver

    def run(self, query: str, **parameters: object) -> tuple[Record, ...]:
        """Record one query and return only the narrow health fixture."""
        self._driver.queries.append((query, dict(parameters)))
        self._driver.authorizations.append(get_authorization(required=False))
        if query == "RETURN 1 AS ok":
            return (Record({"ok": 1}),)
        return ()


class FakeSession:
    """Execute managed Neo4j read callbacks without network access."""

    def __init__(self, driver: FakeDriver) -> None:
        """Bind the driver used for transaction capture."""
        self._driver = driver

    def execute_read(self, work: Any) -> object:
        """Invoke one managed read callback."""
        return work(FakeTransaction(self._driver))

    def run(self, query: str, **parameters: object) -> tuple[Record, ...]:
        """Run the health probe without managed-transaction retries."""
        return FakeTransaction(self._driver).run(query, **parameters)

    def execute_write(self, work: Any) -> object:
        """Invoke one managed write callback."""
        return work(FakeTransaction(self._driver))

    def __enter__(self) -> Self:
        """Enter the fake managed session."""
        return self

    def __exit__(self, *args: object) -> None:
        """Leave the fake session without additional resources."""


class FakeDriver:
    """Provide the Neo4j driver subset needed by the startup integration."""

    def __init__(self) -> None:
        """Initialize query, authorization, session, and close observations."""
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.authorizations: list[AuthorizationContext | None] = []
        self.session_options: list[dict[str, object]] = []
        self.close_count = 0

    def session(self, **options: object) -> FakeSession:
        """Open one fake session and retain its database route."""
        self.session_options.append(dict(options))
        return FakeSession(self)

    def close(self) -> None:
        """Count driver closure at application shutdown."""
        self.close_count += 1


class CapturingDriverFactory:
    """Capture Neo4j connection settings without opening a socket."""

    def __init__(self) -> None:
        """Initialize one reusable fake driver and empty call log."""
        self.driver = FakeDriver()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, uri: str, **options: object) -> FakeDriver:
        """Return the fake driver for one startup composition."""
        self.calls.append((uri, dict(options)))
        return self.driver


def _settings(*, database: bool = True) -> Neo4jConnectionConfig:
    """Return one explicit non-default startup target."""
    if database:
        try:
            shared = get_database()
        except RuntimeError:
            shared = initialize_database()
        create_external_tables(shared.engine)
    else:
        close_database()
    return Neo4jConnectionConfig(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        neo4j_uri="bolt://graph.internal:7687",
        neo4j_user="neo4j-reader",
        neo4j_password="graph-secret",
        neo4j_database="tenant_1",
    )


def _auth_config(dsn: str | None = None) -> AuthConfig:
    """Return one fail-closed auth configuration for tests."""
    if dsn is None:
        dsn = get_settings().db.url
    get_settings().db.url = dsn
    return AuthConfig(
        secret_keys=["test-auth-secret"],
        control_plane_dsn=dsn,
        env="development",
    )


def _seed_member(dsn: str) -> None:
    """Create the current tenant, workspace, member, and role binding."""
    init_schema(dsn)
    create_external_tables(CMDatabase.from_settings(DbSettings(url=dsn)).engine)
    with get_db(dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES (?, ?, ?)",
            ("tenant-1", "Tenant 1", "tenant_1"),
        )
        connection.execute(
            "INSERT INTO workspaces (id, tenant_id, name) VALUES (?, ?, ?)",
            ("workspace-1", "tenant-1", "Workspace 1"),
        )
        connection.execute(
            "INSERT INTO users (id, tenant_id, username) VALUES (?, ?, ?)",
            ("user-1", "tenant-1", "user-1"),
        )
        connection.execute(
            """
            INSERT INTO role_bindings
                (id, user_id, role, scope_type, scope_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("binding-1", "user-1", "member", "workspace", "workspace-1"),
        )
        connection.commit()


async def _noop_initializer() -> None:
    """Seed the declared external schema for isolated service tests."""
    create_external_tables(get_database().engine)


def test_production_app_resolves_runtime_from_authenticated_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph routes bind lazily while pure semantic routes need no graph instance."""
    dsn = f"sqlite:///{tmp_path / 'control-plane.db'}"
    _seed_member(dsn)
    monkeypatch.setenv(
        "DYNAMIC_WORKSPACE_SECRET",
        '{"user":"workspace-user","password":"workspace-secret",'
        '"database":"workspace-database"}',
    )
    factory = CapturingDriverFactory()
    application = create_service_app(
        auth_config=_auth_config(dsn),
        driver_factory=factory,
        semantic_initializer=_noop_initializer,
    )

    @application.get("/api/semantic-config/runtime-binding-test")
    def pure_semantic_route(request: Request) -> dict[str, bool]:
        """Expose whether pure semantic requests unnecessarily selected a graph."""
        return {
            "runtime_bound": hasattr(request.state, "context_runtime"),
        }

    token = TokenIssuer("test-auth-secret").issue_access(
        "user-1",
        "tenant-1",
        "workspace-1",
        "member",
    )
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(application) as client:
        health = client.get("/api/health")
        semantic = client.get(
            "/api/semantic-config/runtime-binding-test",
            headers=headers,
        )
        pending_graph = client.post(
            "/api/v1/graph/list_nodes",
            json={"node": "Metric"},
            headers=headers,
        )
        model_config = client.get("/api/v1/config", headers=headers)
        assert model_config.status_code == 200
        assert factory.calls == []
        with get_db(dsn) as connection:
            connection.execute(
                """
                UPDATE workspaces
                SET endpoint_ref = ?, secret_ref = ?, instance_status = 'ready'
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    "bolt://workspace.internal:7687",
                    "env://DYNAMIC_WORKSPACE_SECRET",
                    "workspace-1",
                    "tenant-1",
                ),
            )
            connection.commit()
        graph = client.post(
            "/api/v1/graph/list_nodes",
            json={"node": "Metric"},
            headers=headers,
        )
        cached_graph = client.post(
            "/api/v1/graph/list_nodes",
            json={"node": "Metric"},
            headers=headers,
        )

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert semantic.status_code == 200
    assert semantic.json() == {"runtime_bound": False}
    assert pending_graph.status_code == 503
    assert pending_graph.json() == {"detail": "workspace graph instance is not ready"}
    assert graph.status_code == 200
    assert cached_graph.status_code == 200
    assert type(application.state.context_runtime) is ContextRuntime
    assert factory.calls == [
        (
            "bolt://workspace.internal:7687",
            {
                "auth": ("workspace-user", "workspace-secret"),
                "notifications_min_severity": "OFF",
            },
        )
    ]
    assert factory.driver.session_options
    assert all(
        options.get("database") == "workspace-database"
        for options in factory.driver.session_options
    )
    assert {"database": "workspace-database"} in factory.driver.session_options
    assert {
        "database": "workspace-database",
        "connection_acquisition_timeout": 2.0,
    } in factory.driver.session_options
    assert (
        sum(
            "n_scope_workspace_id" in parameters
            for _query, parameters in factory.driver.queries
        )
        == 2
    )
    assert factory.driver.close_count == 1
