# -*- coding: utf-8 -*-
"""Host auth gate: local single-user default and multi-user negatives."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from qwenpaw_data_auth import AuthConfig, TokenIssuer, bootstrap_local
from qwenpaw_data_auth.database import (
    get_db,
    new_id,
    reset_engines,
    role_bindings,
    tenants,
    users,
    workspaces,
)

from qwenpaw_data.host.core.api import auth_middleware as gate
from qwenpaw_data.host.core.domain.identity import Identity

SIGNING_KEY = "test-signing-key"
API_TOKEN = "test-api-token"
LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("192.0.2.1", 50000)
LOCAL_ADMIN = {
    "user_id": "__default__",
    "tenant_id": "default",
    "role": "tenant_admin",
    "workspace_id": "default",
    "token": None,
}

_ENV = (
    "QWENPAW_DATA_AUTH_MODE",
    "QWENPAW_DATA_AUTH_SECRET",
    "QWENPAW_DATA_SERVICE_SECRET",
    "QWENPAW_DATA_API_TOKEN",
    "QWENPAW_DATA_AUTH_URL",
    "QWENPAW_DATA_ENV",
    "QWENPAW_DATA_CORS_ALLOW_ORIGINS",
)


@pytest.fixture(autouse=True)
def _isolated_auth(tmp_path, monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL", f"sqlite:///{tmp_path / 'control_plane.db'}"
    )
    monkeypatch.setattr(gate, "_identity_access_tokens", {})
    reset_engines()
    yield
    reset_engines()


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(gate.auth_middleware)

    @app.get("/api/v1/probe")
    async def probe() -> dict:
        principal = gate.get_principal()
        return {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "workspace_id": gate.get_identity().workspace_id,
            "token": gate.get_access_token(),
        }

    return app


@pytest.fixture
def host_app() -> FastAPI:
    from qwenpaw_data.host.core.api.app import create_app

    return create_app()


def _client(
    app: FastAPI, peer: tuple[str, int] = LOOPBACK, cookie: str | None = None
) -> TestClient:
    cookies = {gate.ACCESS_COOKIE: cookie} if cookie else None
    return TestClient(app, base_url="http://localhost", client=peer, cookies=cookies)


def _seed_member(cfg: AuthConfig) -> None:
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(insert(tenants).values(id="t1", name="t1", neo4j_database="neo4j"))
        conn.execute(
            insert(workspaces).values(
                id="w1", tenant_id="t1", name="w1", instance_status="ready"
            )
        )
        conn.execute(
            insert(users).values(
                id="u1", tenant_id="t1", username="alice", active_workspace_id="w1"
            )
        )
        conn.execute(
            insert(role_bindings).values(
                id=new_id(),
                user_id="u1",
                role="member",
                scope_type="workspace",
                scope_id="w1",
            )
        )
        conn.commit()


def _set_user(cfg: AuthConfig, **values) -> None:
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(update(users).where(users.c.id == "u1").values(**values))
        conn.commit()


@pytest.fixture
def multi_user(monkeypatch) -> AuthConfig:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", SIGNING_KEY)
    cfg = bootstrap_local(AuthConfig.from_env())
    _seed_member(cfg)
    return cfg


def _member_jwt() -> str:
    return TokenIssuer(SIGNING_KEY).issue_access("u1", "t1", "w1", "member")


def _forged_admin_jwt() -> str:
    return TokenIssuer("attacker-key").issue_access(
        "__default__", "default", "default", "tenant_admin"
    )


# --- local mode -------------------------------------------------------------


def test_loopback_without_credentials_is_local_admin():
    response = _client(_probe_app()).get("/api/v1/probe")
    assert response.status_code == 200, response.text
    assert response.json() == LOCAL_ADMIN


def test_non_loopback_without_credentials_is_forbidden():
    response = _client(_probe_app(), REMOTE).get("/api/v1/probe")
    assert response.status_code == 403
    assert response.json() == {"detail": "Local access requires a safe loopback client"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "203.0.113.9"},
        {"Forwarded": "for=203.0.113.9"},
        {"X-Forwarded-Host": "evil.example"},
        {"Host": "evil.example"},
        {"Origin": "http://evil.example"},
        {"Referer": "http://evil.example/page"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_unsafe_loopback_request_is_forbidden(headers):
    response = _client(_probe_app()).get("/api/v1/probe", headers=headers)
    assert response.status_code == 403


def test_configured_api_token_is_required_on_loopback(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", API_TOKEN)
    client = _client(_probe_app())
    missing = client.get("/api/v1/probe")
    assert missing.status_code == 401
    assert missing.json() == {"detail": "API token required"}
    for headers in (
        {"Authorization": f"Bearer {API_TOKEN}"},
        {"X-QwenPaw-Data-Api-Token": API_TOKEN},
    ):
        response = client.get("/api/v1/probe", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == LOCAL_ADMIN


def test_api_token_is_a_machine_credential_from_any_peer(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", API_TOKEN)
    response = _client(_probe_app(), REMOTE).get(
        "/api/v1/probe", headers={"Authorization": f"Bearer {API_TOKEN}"}
    )
    assert response.status_code == 200, response.text
    # The static token is never forwarded or recorded as a user token.
    assert response.json() == LOCAL_ADMIN
    assert gate._identity_access_tokens == {}


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token"},
        {"X-QwenPaw-Data-Api-Token": "wrong-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        [("Authorization", f"Bearer {API_TOKEN}"), ("Authorization", f"Bearer {API_TOKEN}")],
    ],
)
def test_wrong_or_ambiguous_api_token_is_rejected(monkeypatch, headers):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", API_TOKEN)
    response = _client(_probe_app()).get("/api/v1/probe", headers=headers)
    assert response.status_code == 401
    assert API_TOKEN not in response.text and "wrong-token" not in response.text


def test_x_user_id_is_rejected_without_service_secret(monkeypatch):
    client = _client(_probe_app())
    assert client.get("/api/v1/probe", headers={"X-User-Id": "u1"}).status_code == 401
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", API_TOKEN)
    response = _client(_probe_app(), REMOTE).get(
        "/api/v1/probe",
        headers={"Authorization": f"Bearer {API_TOKEN}", "X-User-Id": "u1"},
    )
    assert response.status_code == 401


def test_x_user_id_delegation_requires_valid_service_secret():
    cfg = bootstrap_local(AuthConfig.from_env())
    _seed_member(cfg)
    client = _client(_probe_app(), REMOTE)
    delegated = client.get(
        "/api/v1/probe",
        headers={"X-QwenPaw-Data-Service-Secret": cfg.service_secret, "X-User-Id": "u1"},
    )
    assert delegated.status_code == 200, delegated.text
    assert delegated.json() == {
        "user_id": "u1",
        "tenant_id": "t1",
        "role": "member",
        "workspace_id": "w1",
        "token": None,
    }
    forged = client.get(
        "/api/v1/probe",
        headers={"X-QwenPaw-Data-Service-Secret": "wrong", "X-User-Id": "u1"},
    )
    assert forged.status_code == 401


@pytest.mark.parametrize("peer", [LOOPBACK, REMOTE])
def test_forged_jwt_never_falls_back_to_admin(host_app, peer):
    forged = _forged_admin_jwt()
    bearer = {"Authorization": f"Bearer {forged}"}
    for client, headers in (
        (_client(host_app, peer), bearer),
        (_client(host_app, peer, cookie=forged), {}),
    ):
        protected = client.get("/api/v1/auth/me", headers=headers)
        assert protected.status_code == 401
        assert protected.json() == {"detail": "Invalid token"}
        status = client.get("/api/v1/auth/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["authenticated"] is False
        assert status.json()["principal"] is None


def test_auth_status_reports_real_principal_or_anonymous(host_app):
    local = _client(host_app).get("/api/v1/auth/status").json()
    assert local["enabled"] is False and local["authenticated"] is True
    assert local["principal"]["user_id"] == "__default__"
    remote = _client(host_app, REMOTE).get("/api/v1/auth/status").json()
    assert remote["authenticated"] is False and remote["principal"] is None
    assert _client(host_app, REMOTE).get("/api/v1/auth/me").status_code == 403


# --- multi-user mode ---------------------------------------------------------


def test_multi_user_requires_credentials(multi_user, host_app):
    client = _client(host_app)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    status = client.get("/api/v1/auth/status").json()
    assert status["enabled"] is True and status["authenticated"] is False


def test_multi_user_rejects_static_api_token(multi_user, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", API_TOKEN)
    response = _client(_probe_app()).get(
        "/api/v1/probe", headers={"Authorization": f"Bearer {API_TOKEN}"}
    )
    assert response.status_code == 403


def test_multi_user_jwt_is_revalidated_every_request(multi_user, host_app):
    token = _member_jwt()
    probe = _client(_probe_app(), REMOTE)
    bearer = probe.get("/api/v1/probe", headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 200, bearer.text
    assert bearer.json() == {
        "user_id": "u1",
        "tenant_id": "t1",
        "role": "member",
        "workspace_id": "w1",
        "token": token,
    }
    assert gate.get_identity_access_token(Identity("u1", "t1", "w1")) == token
    cookie = _client(_probe_app(), REMOTE, cookie=token).get("/api/v1/probe")
    assert cookie.status_code == 200 and cookie.json()["token"] == token
    status = _client(host_app).get(
        "/api/v1/auth/status", headers={"Authorization": f"Bearer {token}"}
    )
    assert status.json()["principal"]["user_id"] == "u1"

    _set_user(multi_user, status="disabled")
    disabled = probe.get("/api/v1/probe", headers={"Authorization": f"Bearer {token}"})
    assert disabled.status_code == 403


def test_multi_user_revoked_token_is_rejected(multi_user):
    token = _member_jwt()
    revoked_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    _set_user(multi_user, token_revoked_before=revoked_at.isoformat())
    response = _client(_probe_app()).get(
        "/api/v1/probe", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Token revoked"}


# --- exemptions, CORS, startup ----------------------------------------------


def test_health_and_preflight_are_exempt(host_app):
    client = _client(host_app, REMOTE)
    assert client.get("/health").json() == {"status": "ok"}
    forged = {"Authorization": f"Bearer {_forged_admin_jwt()}"}
    assert client.get("/health", headers=forged).status_code == 200
    preflight = client.options(
        "/api/v1/auth/me",
        headers={"Origin": "http://localhost", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200
    assert _client(_probe_app(), REMOTE).options("/api/v1/probe").status_code == 405


def test_rejections_carry_cors_headers(host_app):
    response = _client(host_app, REMOTE).get(
        "/api/v1/auth/me", headers={"Origin": "http://localhost"}
    )
    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == "http://localhost"


def test_unbound_context_fails_closed():
    assert gate.get_principal() is gate.ANONYMOUS_PRINCIPAL
    assert not gate.is_authenticated()
    with pytest.raises(HTTPException) as exc:
        gate.get_identity()
    assert exc.value.status_code == 401


async def test_lifespan_bootstraps_identity_before_host_schema(monkeypatch):
    from qwenpaw_data.host.core.api import app as app_module

    calls: list[str] = []
    real_bootstrap = app_module.bootstrap_local

    def bootstrap(cfg):
        calls.append("auth")
        return real_bootstrap(cfg)

    async def init_db():
        calls.append("host")
        raise RuntimeError("stop after init_db")

    monkeypatch.setattr(app_module, "bootstrap_local", bootstrap)
    monkeypatch.setattr(app_module, "init_db", init_db)
    app = app_module.create_app()
    with pytest.raises(RuntimeError, match="stop after init_db"):
        async with app_module.lifespan(app):
            pass
    assert calls == ["auth", "host"]
    assert app.state.auth_config.local_mode


def test_startup_configuration_by_mode(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", SIGNING_KEY)
    gate.validate_startup_configuration()  # local mode needs no Auth URL/secret
    assert gate.auth_enabled() is False

    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    assert gate.auth_enabled() is True
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_AUTH_URL"):
        gate.validate_startup_configuration()
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://127.0.0.1:8090")
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_SERVICE_SECRET"):
        gate.validate_startup_configuration()
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "test-service-secret")
    gate.validate_startup_configuration()

    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "local")
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET")
    monkeypatch.setenv("QWENPAW_DATA_ENV", "production")
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_AUTH_SECRET"):
        gate.validate_startup_configuration()
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "anonymous")
    with pytest.raises(ValueError, match="QWENPAW_DATA_AUTH_MODE"):
        gate.validate_startup_configuration()
