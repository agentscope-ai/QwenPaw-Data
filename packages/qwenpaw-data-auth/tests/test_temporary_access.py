"""Service-signed access tokens for Host cron / IM workers.

Covers the machine credential gate and the membership and ttl contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data_auth.config import ACCESS_TOKEN_TTL_DEFAULT
from qwenpaw_data_auth.database import get_db, init_schema, new_id
from qwenpaw_data_auth.server import create_app
from qwenpaw_data_auth.token import TokenVerifier

SERVICE_SECRET = "service-test-secret"
SIGNING_SECRET = "test-secret"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    dsn = f"sqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", dsn)
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    init_schema(dsn)
    return dsn


def _seed(
    dsn: str,
    *,
    tenant_admin: bool = False,
    ws_admin: bool = False,
    status: str = "active",
    tenant_status: str = "active",
) -> str:
    """Create tenant t1 with workspaces ws1/ws2 and one user bound to ws1."""
    user_id = new_id()
    with get_db(dsn) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database, status) VALUES ('t1', 'T1', 'neo4j', ?)",
            (tenant_status,),
        )
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES ('t2', 'T2', 'neo4j2')"
        )
        for ws_id, tenant in (("ws1", "t1"), ("ws2", "t1"), ("ws-other", "t2")):
            conn.execute(
                """
                INSERT INTO workspaces (id, tenant_id, name, instance_status)
                VALUES (?, ?, ?, 'ready')
                """,
                (ws_id, tenant, ws_id.upper()),
            )
        conn.execute(
            """
            INSERT INTO users (id, tenant_id, username, password_hash, status)
            VALUES (?, 't1', 'cronuser', '', ?)
            """,
            (user_id, status),
        )
        conn.execute(
            """
            INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id)
            VALUES (?, ?, 'member', 'workspace', 'ws1')
            """,
            (new_id(), user_id),
        )
        if tenant_admin:
            conn.execute(
                """
                INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id)
                VALUES (?, ?, 'tenant_admin', 'tenant', 't1')
                """,
                (new_id(), user_id),
            )
        if ws_admin:
            conn.execute(
                """
                INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id)
                VALUES (?, ?, 'ws_admin', 'workspace', 'ws1')
                """,
                (new_id(), user_id),
            )
        conn.commit()
    return user_id


def _client() -> TestClient:
    app = create_app()
    return TestClient(app)


def _post(client: TestClient, body: dict, *, secret: str | None = SERVICE_SECRET):
    headers = {} if secret is None else {"X-QwenPaw-Data-Service-Secret": secret}
    return client.post("/api/auth/temporary-access", json=body, headers=headers)


def _claims(token: str) -> dict:
    return TokenVerifier([SIGNING_SECRET]).verify(token)


def test_member_receives_a_short_lived_token(env):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client,
            {
                "user_id": user_id,
                "tenant_id": "t1",
                "workspace_id": "ws1",
                "ttl": 1800,
            },
        )
    assert response.status_code == 200, response.text
    claims = _claims(response.json()["access_token"])
    assert claims["typ"] == "access"
    assert claims["sub"] == user_id
    assert claims["tenant_id"] == "t1"
    assert claims["active_ws"] == "ws1"
    assert claims["role"] == "member"
    assert claims["exp"] - claims["iat"] == pytest.approx(1800, abs=2)
    # A machine caller gets JSON only — no session is established.
    assert "set-cookie" not in response.headers
    assert "acl_grants" not in claims


def test_ttl_defaults_to_the_configured_access_ttl(env):
    """Omitted ttl uses access_token_ttl."""
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 200, response.text
    claims = _claims(response.json()["access_token"])
    assert claims["exp"] - claims["iat"] == pytest.approx(
        ACCESS_TOKEN_TTL_DEFAULT, abs=2
    )


def test_ttl_defaults_track_an_overridden_ceiling(env, monkeypatch):
    """Omitted ttl follows QWENPAW_DATA_ACCESS_TOKEN_TTL."""
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "7200")
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 200, response.text
    claims = _claims(response.json()["access_token"])
    assert claims["exp"] - claims["iat"] == pytest.approx(7200, abs=2)


def test_tenant_admin_role_is_resolved_not_supplied(env):
    user_id = _seed(env, tenant_admin=True)
    with _client() as client:
        # ws2 has no explicit binding: tenant_admin sees every workspace.
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws2"}
        )
    assert response.status_code == 200, response.text
    assert _claims(response.json()["access_token"])["role"] == "tenant_admin"


def test_issued_token_is_accepted_by_the_session_endpoints(env):
    user_id = _seed(env)
    with _client() as client:
        token = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"}
        ).json()["access_token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["active_workspace_id"] == "ws1"
    assert me.json()["user_id"] == user_id


def test_the_endpoint_leaves_the_active_workspace_alone(env):
    user_id = _seed(env)
    with get_db(env) as conn:
        conn.execute(
            "UPDATE users SET active_workspace_id = 'ws1' WHERE id = ?", (user_id,)
        )
        conn.commit()
    with _client() as client:
        _post(client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws2"})
    with get_db(env) as conn:
        row = conn.execute(
            "SELECT active_workspace_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    assert row["active_workspace_id"] == "ws1"


@pytest.mark.parametrize("secret", [None, "", "wrong-secret"])
def test_a_bad_service_secret_is_refused(env, secret):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client,
            {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"},
            secret=secret,
        )
    assert response.status_code == 403


def test_a_workspace_without_a_binding_is_refused(env):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws2"}
        )
    assert response.status_code == 403


def test_another_tenants_workspace_is_refused(env):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws-other"}
        )
    assert response.status_code == 403


def test_a_mismatched_tenant_is_refused(env):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t2", "workspace_id": "ws-other"}
        )
    assert response.status_code == 403


def test_a_suspended_tenant_is_refused(env):
    user_id = _seed(env, tenant_status="suspended")
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 403


def test_a_disabled_user_is_not_found(env):
    user_id = _seed(env, status="disabled")
    with _client() as client:
        response = _post(
            client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 404


def test_an_unknown_user_is_not_found(env):
    _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": "nobody", "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 404


@pytest.mark.parametrize("ttl", [0, -1, 86401])
def test_an_out_of_range_ttl_is_rejected(env, ttl):
    user_id = _seed(env)
    with _client() as client:
        response = _post(
            client,
            {
                "user_id": user_id,
                "tenant_id": "t1",
                "workspace_id": "ws1",
                "ttl": ttl,
            },
        )
    assert response.status_code == 400
    assert "86400" in response.json()["detail"]


def test_blank_identity_fields_are_rejected(env):
    _seed(env)
    with _client() as client:
        response = _post(
            client, {"user_id": "  ", "tenant_id": "t1", "workspace_id": "ws1"}
        )
    assert response.status_code == 400


def test_an_unconfigured_service_secret_still_gates_the_endpoint(
    env, monkeypatch: pytest.MonkeyPatch
):
    # Without QWENPAW_DATA_SERVICE_SECRET the server generates and persists one
    # under QWENPAW_DATA_HOME, so the endpoint never opens up to callers.
    user_id = _seed(env)
    monkeypatch.delenv("QWENPAW_DATA_SERVICE_SECRET")
    with _client() as client:
        for secret in (None, SERVICE_SECRET):
            response = _post(
                client,
                {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"},
                secret=secret,
            )
            assert response.status_code == 403


def test_issuance_is_audited(env):
    user_id = _seed(env)
    with _client() as client:
        _post(client, {"user_id": user_id, "tenant_id": "t1", "workspace_id": "ws1"})
    with get_db(env) as conn:
        row = conn.execute(
            "SELECT event_type, target_id, detail FROM audit_logs WHERE actor_id = ?",
            (user_id,),
        ).fetchone()
    assert row["event_type"] == "service_access_issued"
    assert row["target_id"] == "ws1"
    assert '"role": "member"' in row["detail"]
