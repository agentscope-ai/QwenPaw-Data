"""New Workspace access triggers a committed, self-scoped, best-effort MG pull."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from qwenpaw_data_auth import mg_sync, user_api
from qwenpaw_data_auth.authorization import authorize_request
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, init_schema
from qwenpaw_data_auth.server import create_app
from qwenpaw_data_auth.token import TokenIssuer
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

PASSWORD = "Strong!Pass9"
TOKEN_PATH = "/api/auth/temporary-access"
SYNC_PATH = "/api/v1/mg/userspace/sync"


@pytest.fixture()
def scenario(tmp_path, monkeypatch):
    dsn = f"sqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", dsn)
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "test-service-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test/")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test/")
    init_schema(dsn)
    with get_db(dsn) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES ('t1', 'T1', 'neo4j')"
        )
        for workspace in ("ws1", "ws2"):
            conn.execute(
                "INSERT INTO workspaces (id, tenant_id, name, instance_status) "
                "VALUES (?, 't1', ?, 'ready')",
                (workspace, workspace),
            )
        conn.execute(
            "INSERT INTO users (id, tenant_id, username) VALUES ('creator', 't1', 'creator')"
        )
        for workspace in ("ws1", "ws2"):
            conn.execute(
                "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
                "VALUES (?, 'creator', 'ws_admin', 'workspace', ?)",
                (workspace, workspace),
            )
        conn.commit()

    app = create_app()
    state = SimpleNamespace(dsn=dsn, calls=[], synced=[], failure=None, sent=[])

    @app.post(SYNC_PATH)
    async def sync(request: Request):
        ctx = authorize_request(request)
        state.synced.append((ctx.tenant_id, ctx.active_workspace_id, ctx.user_id))
        return {"ok": True, "data": {"conflict_count": 0}}

    transport = httpx.ASGITransport(app=app)

    async def dispatch(request):
        state.calls.append(request)
        # Background work must start after the response has been fully sent.
        assert state.sent[-1]["type"] == "http.response.body"
        assert not state.sent[-1].get("more_body", False)
        if state.failure and request.url.path == state.failure[0]:
            if state.failure[1] == "timeout":
                raise httpx.ReadTimeout("test timeout", request=request)
            return httpx.Response(state.failure[1], json={"detail": "unavailable"})
        return await transport.handle_async_request(request)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        mg_sync.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(dispatch), **kwargs
        ),
    )

    async def capture_response(scope, receive, send):
        async def capture(message):
            state.sent.append(message)
            await send(message)

        await app(scope, receive, capture)

    token = TokenIssuer("test-secret").issue_access("creator", "t1", "ws1", "ws_admin")
    with TestClient(capture_response) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        state.client = client
        state.app = app
        yield state


def _create(scenario, **overrides):
    return scenario.client.post(
        "/api/users",
        json={
            "username": "new-member",
            "password": PASSWORD,
            "workspace_id": "ws2",
            **overrides,
        },
    )


def test_new_user_sync_runs_after_response_as_the_new_user(scenario):
    response = _create(scenario)
    assert response.status_code == 201, response.text
    user_id = response.json()["user"]["id"]
    assert scenario.synced == [("t1", "ws2", user_id)]
    token_request, sync_request = scenario.calls
    assert str(token_request.url) == f"http://auth.test{TOKEN_PATH}"
    assert json.loads(token_request.content) == {
        "tenant_id": "t1",
        "workspace_id": "ws2",
        "user_id": user_id,
    }
    assert token_request.headers["X-QwenPaw-Data-Service-Secret"] == "test-service-secret"
    assert str(sync_request.url) == f"http://cm.test{SYNC_PATH}"
    assert "X-QwenPaw-Data-Service-Secret" not in sync_request.headers
    assert "cookie" not in sync_request.headers
    # The real temporary-access handler above verified the committed account and binding.
    assert scenario.client.get("/api/auth/me").json()["user_id"] == "creator"


@pytest.mark.parametrize(
    "failure",
    [
        (TOKEN_PATH, 403),
        (TOKEN_PATH, "timeout"),
        (SYNC_PATH, 503),
        (SYNC_PATH, "timeout"),
    ],
)
def test_failed_sync_does_not_undo_creation_or_retry(scenario, failure, caplog):
    scenario.failure = failure
    response = _create(scenario)
    assert response.status_code == 201
    user_id = response.json()["user"]["id"]
    with get_db(scenario.dsn) as conn:
        assert conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        assert conn.execute(
            "SELECT id FROM role_bindings WHERE user_id = ? AND scope_id = 'ws2'",
            (user_id,),
        ).fetchone()
    assert len(scenario.calls) == (1 if failure[0] == TOKEN_PATH else 2)
    assert "Initial MG sync failed" in caplog.text
    assert "test-service-secret" not in caplog.text


@pytest.mark.parametrize(
    "role, workspaces", [("member", ["ws2"]), ("tenant_admin", ["ws1", "ws2"])]
)
def test_tenant_admin_creation_syncs_all_new_workspaces(scenario, role, workspaces):
    with get_db(scenario.dsn) as conn:
        conn.execute(
            "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
            "VALUES ('tenant-role', 'creator', 'tenant_admin', 'tenant', 't1')"
        )
        conn.commit()
    response = _create(scenario, role=role)
    assert response.status_code == 201
    assert scenario.synced == [
        ("t1", workspace, response.json()["user"]["id"]) for workspace in workspaces
    ]


@pytest.mark.parametrize(
    "fields, status",
    [
        ({"role": "ws_admin"}, 403),
        ({"workspace_id": "other"}, 403),
        ({"username": "creator"}, 409),
    ],
)
def test_rejected_creation_does_not_trigger_sync(scenario, fields, status):
    assert _create(scenario, **fields).status_code == status
    assert scenario.calls == []


def test_missing_service_configuration_keeps_creation_successful(scenario, caplog):
    scenario.app.state.auth_config = replace(
        scenario.app.state.auth_config, service_secret=""
    )
    assert _create(scenario).status_code == 201
    assert scenario.calls == []
    assert "Initial MG sync skipped" in caplog.text


def test_rolled_back_creation_does_not_trigger_sync(scenario, monkeypatch):
    def fail_binding(*args):
        raise IntegrityError("insert binding", None, Exception("test failure"))

    monkeypatch.setattr(user_api, "_ensure_role_binding", fail_binding)
    assert _create(scenario).status_code == 409
    assert scenario.calls == []
    with get_db(scenario.dsn) as conn:
        assert (
            conn.execute(
                "SELECT id FROM users WHERE username = 'new-member'"
            ).fetchone()
            is None
        )


def test_service_urls_use_existing_environment_settings(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", " http://auth.test/ ")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", " http://cm.test/ ")
    cfg = AuthConfig.from_env()
    assert cfg.auth_url == "http://auth.test"
    assert cfg.cm_base_url == "http://cm.test"
    monkeypatch.delenv("QWENPAW_DATA_CM_BASE_URL")
    assert AuthConfig.from_env().cm_base_url == "http://127.0.0.1:8765"


@pytest.mark.parametrize("existing_workspace", [None, "ws1", "ws2"])
@pytest.mark.parametrize("target_type", ["workspace", "tenant"])
def test_approval_syncs_only_new_access(scenario, existing_workspace, target_type):
    with get_db(scenario.dsn) as conn:
        conn.execute("UPDATE workspaces SET join_open = 1 WHERE id = 'ws2'")
        conn.execute("UPDATE tenants SET join_open = 1 WHERE id = 't1'")
        conn.execute(
            "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
            "VALUES ('tenant-role', 'creator', 'tenant_admin', 'tenant', 't1')"
        )
        conn.execute(
            "INSERT INTO users (id, tenant_id, username) "
            "VALUES ('applicant', 't1', 'applicant')"
        )
        if existing_workspace:
            conn.execute(
                "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
                "VALUES ('old-seat', 'applicant', 'member', 'workspace', ?)",
                (existing_workspace,),
            )
        conn.execute(
            "INSERT INTO access_requests (id, user_id, target_type, target_id, tenant_id) "
            "VALUES ('request', 'applicant', ?, ?, 't1')",
            (target_type, "ws2" if target_type == "workspace" else "t1"),
        )
        conn.commit()
    response = scenario.client.post(
        "/api/access-requests/request/approve", json={"workspace_id": "ws2"}
    )
    assert response.status_code == 200, response.text
    user_id = response.json()["user_id"]
    expected = [] if existing_workspace == "ws2" else [("t1", "ws2", user_id)]
    assert scenario.synced == expected
    with get_db(scenario.dsn) as conn:
        assert (
            conn.execute(
                "SELECT status FROM access_requests WHERE id = 'request'"
            ).fetchone()["status"]
            == "approved"
        )
    assert (
        scenario.client.post("/api/access-requests/request/approve").status_code == 409
    )
    assert scenario.synced == expected


def test_direct_role_grants_sync_only_new_workspace_access(scenario):
    with get_db(scenario.dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, tenant_id, username) VALUES ('member', 't1', 'member')"
        )
        conn.execute(
            "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
            "VALUES ('tenant-role', 'creator', 'tenant_admin', 'tenant', 't1')"
        )
        conn.commit()

    def grant(role, scope_type, scope_id):
        return scenario.client.post(
            "/api/roles/bindings",
            json={
                "user_id": "member",
                "role": role,
                "scope_type": scope_type,
                "scope_id": scope_id,
            },
        )

    assert grant("member", "workspace", "ws1").status_code == 201
    assert scenario.synced == [("t1", "ws1", "member")]
    assert grant("ws_admin", "workspace", "ws1").status_code == 201
    assert scenario.synced == [("t1", "ws1", "member")]
    assert grant("tenant_admin", "tenant", "t1").status_code == 201
    assert scenario.synced == [("t1", "ws1", "member"), ("t1", "ws2", "member")]
    assert grant("tenant_admin", "tenant", "t1").status_code == 409
    assert len(scenario.synced) == 2
