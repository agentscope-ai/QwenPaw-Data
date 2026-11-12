"""Cap MCP tools and REST routes by scoped API keys and RBAC, failing closed."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.auth import ALL_SCOPES, SCOPE_QUERY, SCOPE_WRITE
from context_manager.api.authentication import (
    install_context_authentication,
    rbac_scopes,
)
from context_manager.schema import GRAPH_SCHEMA
from context_manager.sql import RdbConnector
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from qwenpaw_data_auth.authorization import AuthorizationContext
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, role_bindings
from qwenpaw_data_auth.local import bootstrap_local
from qwenpaw_data_auth.principal import DEFAULT_PRINCIPAL
from sqlalchemy import update

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore
from cm.retrieval.mcp_support import ACCEPT, call_tool, rpc

READER = "reader-token-0123456789"
WRITER = "writer-token-0123456789"
WRITE_ONLY = "write-only-token-0123456789"
ADMIN = "admin-token-0123456789"
API_KEYS = json.dumps(
    [
        {"id": "reader", "token": READER, "scopes": ["query"]},
        {"id": "writer", "token": WRITER, "scopes": ["query", "write"]},
        {"id": "write-only", "token": WRITE_ONLY, "scopes": ["write"]},
        {"id": "admin", "token": ADMIN, "scopes": sorted(ALL_SCOPES)},
    ]
)
SQL_WRITES = [
    ("execute_create", "CREATE TABLE t(x INT)"),
    ("execute_modify", "INSERT INTO t VALUES (1)"),
]
LOOPBACK = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
PROBE = "/api/cm/__principal"


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_API_KEYS", API_KEYS)


def _principal(request: Request) -> dict:
    principal = request.state.auth_principal
    return {
        "subject": principal.subject,
        "scopes": sorted(principal.scopes),
        "credential_id": principal.credential_id,
    }


def _app(tmp_path, *, mode="local", authorizer=None):
    """Mount the default Workspace behind real or injected authorization."""
    store = MemoryGraphStore()
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        tenant_id="default",
        workspace_id="default",
    )
    store._runtime = SimpleNamespace(target=runtime.fixed_target)
    app = create_api_app()
    bind_context_runtime(app, runtime)
    # An unclassified managed route that reports the bound principal.
    app.router.routes.insert(0, APIRoute(PROBE, _principal, methods=["GET"]))
    config = bootstrap_local(
        AuthConfig(
            mode=mode,
            home=tmp_path / "auth-home",
            control_plane_dsn=str(tmp_path / "control_plane.db"),
        )
    )
    options = {} if authorizer is None else {"authorizer": authorizer}
    install_context_authentication(app, config, runtime.fixed_target, **options)
    return app, runtime, config


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("tool,sql", SQL_WRITES)
def test_read_key_cannot_call_sql_write_tools(tmp_path, tool, sql):
    app, runtime, _ = _app(tmp_path)
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        response = call_tool(
            client, tool, {"datasource_id": "warehouse", "sql": sql}, token=READER
        )
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
        assert "Insufficient tool scope" in response.text
        service.assert_not_called()


@pytest.mark.parametrize("tool,sql", SQL_WRITES)
def test_write_key_reaches_sql_write_tools(tmp_path, tool, sql):
    """Positive control: the same call passes the scope gate with a write key."""
    app, runtime, _ = _app(tmp_path)
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        response = call_tool(
            client, tool, {"datasource_id": "warehouse", "sql": sql}, token=WRITER
        )
        assert response.status_code == 200
        assert "Insufficient tool scope" not in response.text
        service.assert_called_once()


def test_read_key_can_call_query_tools(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app, **LOOPBACK) as client:
        response = call_tool(client, "get_graph_schema", token=READER)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


def test_key_without_query_scope_cannot_open_mcp(tmp_path):
    app, runtime, _ = _app(tmp_path)
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        response = call_tool(
            client,
            "execute_create",
            {"datasource_id": "warehouse", "sql": "CREATE TABLE t(x INT)"},
            token=WRITE_ONLY,
        )
        assert response.status_code == 403
        assert response.json()["required_scopes"] == ["query"]
        service.assert_not_called()


@pytest.mark.parametrize(
    "method,path,required",
    [
        ("POST", "/api/v1/admin/cypher", ["manage"]),
        ("PUT", "/api/v1/cm/datasources/active", ["manage"]),
        ("GET", "/api/system/model-config", ["credentials:manage"]),
    ],
)
def test_read_key_cannot_use_privileged_routes(tmp_path, method, path, required):
    app, _, _ = _app(tmp_path)
    with TestClient(app, **LOOPBACK) as client:
        response = client.request(method, path, headers=_bearer(READER))
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Insufficient scope",
        "required_scopes": required,
    }
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


def test_key_capped_principals_fail_closed_on_unclassified_routes(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app, **LOOPBACK) as client:
        response = client.get(PROBE, headers=_bearer(ADMIN))
    assert response.status_code == 403
    assert response.json() == {"detail": "No authorization policy for this endpoint"}


def test_scoped_keys_never_exceed_the_local_identity_rbac(tmp_path):
    app, runtime, config = _app(tmp_path)
    with get_db(config.control_plane_dsn) as conn:
        conn.execute(
            update(role_bindings)
            .where(role_bindings.c.user_id == DEFAULT_PRINCIPAL.user_id)
            .values(role="member", scope_type="workspace", scope_id="default")
        )
        conn.commit()
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        cypher = client.post("/api/v1/admin/cypher", headers=_bearer(ADMIN))
        write = call_tool(
            client,
            "execute_create",
            {"datasource_id": "warehouse", "sql": "CREATE TABLE t(x INT)"},
            token=ADMIN,
        )
        assert cypher.status_code == 403
        assert cypher.json()["required_scopes"] == ["manage"]
        assert write.json()["result"]["isError"] is True
        assert "administration" in write.text
        service.assert_not_called()


@pytest.mark.parametrize("token", [None, "invalid-token-0123456789"])
def test_configured_keys_deny_missing_or_invalid_credentials(tmp_path, token):
    app, runtime, _ = _app(tmp_path)
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        tool = call_tool(
            client,
            "execute_create",
            {"datasource_id": "warehouse", "sql": "CREATE TABLE t(x INT)"},
            token=token,
        )
        rest = client.get(PROBE, headers=_bearer(token) if token else {})
        assert tool.status_code == 401
        assert rest.status_code == 401
        service.assert_not_called()


def test_local_loopback_admin_without_keys_has_full_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_KEYS")
    app, runtime, _ = _app(tmp_path)
    with (
        TestClient(app, **LOOPBACK) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        principal = client.get(PROBE)
        write = call_tool(
            client,
            "execute_create",
            {"datasource_id": "warehouse", "sql": "CREATE TABLE t(x INT)"},
            token=None,
        )
        assert principal.status_code == 200
        assert principal.json() == {
            "subject": DEFAULT_PRINCIPAL.user_id,
            "scopes": sorted(ALL_SCOPES),
            "credential_id": "local",
        }
        assert "Insufficient tool scope" not in write.text
        service.assert_called_once()


def test_scoped_keys_require_a_safe_loopback_client(tmp_path):
    app, _, _ = _app(tmp_path)
    remote = {**LOOPBACK, "client": ("203.0.113.7", 50000)}
    with TestClient(app, **remote) as client:
        response = call_tool(client, "get_graph_schema", token=READER)
    assert response.status_code == 403


def test_scoped_keys_are_disabled_in_multi_user_mode(tmp_path):
    app, _, _ = _app(tmp_path, mode="multi-user")
    with TestClient(app, **LOOPBACK) as client:
        response = call_tool(client, "get_graph_schema", token=READER)
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Scoped API keys are disabled in multi-user mode"
    }


@pytest.mark.parametrize(
    "headers",
    [
        {"Cookie": "qwenpaw_data_access=session"},
        {"X-User-Id": "user-2"},
        {"X-QwenPaw-Data-Service-Secret": "service-secret"},
        {"X-QwenPaw-Data-Api-Token": "local-api-token"},
    ],
)
def test_scoped_keys_reject_conflicting_credentials(tmp_path, headers):
    app, _, _ = _app(tmp_path)
    with TestClient(app, **LOOPBACK) as client:
        response = call_tool(client, "get_graph_schema", token=READER, **headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Conflicting credentials"}


def test_scoped_keys_reject_duplicate_authorization_headers(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app, **LOOPBACK) as client:
        response = client.post(
            "/mcp/v1/cm",
            headers=[
                *ACCEPT.items(),
                ("Authorization", f"Bearer {READER}"),
                ("Authorization", f"Bearer {WRITER}"),
            ],
            json=rpc("tools/call", params={"name": "get_graph_schema", "arguments": {}}),
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "role,scopes",
    [
        ("member", [SCOPE_QUERY, SCOPE_WRITE]),
        ("ws_admin", sorted(ALL_SCOPES)),
        ("tenant_admin", sorted(ALL_SCOPES)),
    ],
)
def test_session_principals_carry_rbac_scopes(tmp_path, monkeypatch, role, scopes):
    monkeypatch.delenv("QWENPAW_DATA_API_KEYS")

    def authorizer(request: Request) -> AuthorizationContext:
        assert request.headers["authorization"] == "Bearer session-token"
        return authorization("user-9", "default", "default", role=role)

    app, _, _ = _app(tmp_path, mode="multi-user", authorizer=authorizer)
    with TestClient(app) as client:
        response = client.get(PROBE, headers=_bearer("session-token"))
    assert response.status_code == 200
    assert response.json() == {
        "subject": "user-9",
        "scopes": scopes,
        "credential_id": "session",
    }


def test_rbac_scopes_follow_the_active_workspace_role():
    assert rbac_scopes(authorization(role="member")) == {SCOPE_QUERY, SCOPE_WRITE}
    assert rbac_scopes(authorization(role="ws_admin")) == ALL_SCOPES
    assert rbac_scopes(authorization(role="tenant_admin")) == ALL_SCOPES
    assert rbac_scopes(authorization(role="viewer")) == frozenset()
    platform = AuthorizationContext(
        "operator", None, "platform_admin", "platform_admin", is_platform_admin=True
    )
    assert rbac_scopes(platform) == frozenset()
