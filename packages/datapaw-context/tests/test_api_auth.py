"""api.auth API token 认证中间件测试。"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from context_manager.api.auth import (
    API_KEYS_ENV,
    CLIENT_API_TOKEN_ENV,
    SCOPE_CREDENTIALS,
    SCOPE_MANAGE,
    SCOPE_QUERY,
    SCOPE_WRITE,
    authenticate_api_token,
    configured_bearer_headers,
    install_api_token_auth,
    internal_callback_auth_headers,
)


@pytest.fixture(autouse=True)
def _clear_scoped_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    monkeypatch.delenv(CLIENT_API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(
        "DATAPAW_SECURITY_AUDIT_LOG",
        str(tmp_path / "security-audit.jsonl"),
    )
    monkeypatch.setenv("DATAPAW_API_ALLOWED_HOSTS", "testserver")


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/cm/domains")
    async def protected() -> dict[str, str]:
        return {"data": "secret"}

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, bool]:
        return {"required": True}

    @app.get("/api/auth/check")
    async def auth_check() -> dict[str, bool]:
        return {"authenticated": True}

    @app.post("/api/feedback")
    async def write_data() -> dict[str, bool]:
        return {"written": True}

    @app.post("/api/admin/reset_memory")
    async def manage_system() -> dict[str, bool]:
        return {"managed": True}

    @app.get("/api/semantic-config/datasource")
    async def read_credentials() -> dict[str, bool]:
        return {"credentials": True}

    @app.get("/api/new-unclassified")
    async def unclassified() -> dict[str, bool]:
        return {"unsafe": True}

    return app


def test_disabled_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAPAW_API_TOKEN", raising=False)
    app = _build_app()
    assert install_api_token_auth(app) is False
    client = TestClient(app)
    assert client.get("/api/v1/cm/domains").status_code == 200

    # The middleware remains installed so adding a token at runtime cannot
    # leave the status endpoint and actual protection out of sync.
    monkeypatch.setenv("DATAPAW_API_TOKEN", "enabled-later")
    assert client.get("/api/v1/cm/domains").status_code == 401
    assert client.get(
        "/api/v1/cm/domains",
        headers={"Authorization": "Bearer enabled-later"},
    ).status_code == 200


def test_rejects_missing_and_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "s3cret-token")
    app = _build_app()
    assert install_api_token_auth(app) is True
    client = TestClient(app)

    resp = client.get("/api/v1/cm/domains")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"

    resp = client.get(
        "/api/v1/cm/domains", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401

    # 非 Bearer scheme 一律拒绝
    resp = client.get(
        "/api/v1/cm/domains", headers={"Authorization": "Basic s3cret-token"}
    )
    assert resp.status_code == 401


def test_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "s3cret-token")
    app = _build_app()
    install_api_token_auth(app)
    client = TestClient(app)
    resp = client.get(
        "/api/v1/cm/domains", headers={"Authorization": "Bearer s3cret-token"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"data": "secret"}


def test_health_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "s3cret-token")
    app = _build_app()
    install_api_token_auth(app)
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/auth/check").status_code == 401


def test_exempt_paths_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "s3cret-token")
    app = _build_app()

    @app.get("/api/health/details")
    async def health_details() -> dict[str, str]:
        return {"data": "sensitive"}

    install_api_token_auth(app)
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/details").status_code == 401


def test_internal_auth_headers_never_leak_to_external_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "callback-secret")
    assert configured_bearer_headers() == {
        "Authorization": "Bearer callback-secret"
    }
    assert internal_callback_auth_headers(
        "http://127.0.0.1:8765/api/semantic-config/weave-task/callback"
    ) == {"Authorization": "Bearer callback-secret"}
    assert internal_callback_auth_headers(
        "http://localhost:8765/api/semantic-config/weave-task/callback?task=1"
    ) == {"Authorization": "Bearer callback-secret"}
    assert internal_callback_auth_headers(
        "https://example.com/api/semantic-config/weave-task/callback"
    ) == {}
    assert internal_callback_auth_headers(
        "http://127.0.0.1:9999/api/semantic-config/weave-task/callback"
    ) == {}
    assert internal_callback_auth_headers("http://127.0.0.1:8765/other") == {}


def _scoped_keys() -> tuple[str, dict[str, str]]:
    tokens = {
        "reader": "reader-token-1234567890",
        "writer": "writer-token-1234567890",
        "manager": "manager-token-12345678",
        "credentials": "credentials-token-1234",
    }
    registry = [
        {"id": name, "token": token, "scopes": [scope]}
        for (name, token), scope in zip(
            tokens.items(),
            (SCOPE_QUERY, SCOPE_WRITE, SCOPE_MANAGE, SCOPE_CREDENTIALS),
            strict=True,
        )
    ]
    return json.dumps(registry), tokens


def test_scoped_keys_are_authorized_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, tokens = _scoped_keys()
    monkeypatch.delenv("DATAPAW_API_TOKEN", raising=False)
    monkeypatch.setenv(API_KEYS_ENV, registry)
    app = _build_app()
    assert install_api_token_auth(app, enforce_scopes=True) is True
    client = TestClient(app)

    def headers(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[name]}"}

    assert client.get("/api/v1/cm/domains", headers=headers("reader")).status_code == 200
    denied = client.post("/api/feedback", headers=headers("reader"))
    assert denied.status_code == 403
    assert denied.json()["required_scopes"] == [SCOPE_WRITE]
    assert client.post("/api/feedback", headers=headers("writer")).status_code == 200
    assert client.post(
        "/api/admin/reset_memory", headers=headers("manager")
    ).status_code == 200
    assert client.get(
        "/api/semantic-config/datasource", headers=headers("credentials")
    ).status_code == 200
    assert client.get("/api/new-unclassified", headers=headers("reader")).status_code == 403


def test_hashed_api_key_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "hashed-reader-token-123456"
    monkeypatch.delenv("DATAPAW_API_TOKEN", raising=False)
    monkeypatch.setenv(
        API_KEYS_ENV,
        json.dumps(
            [
                {
                    "id": "hashed-reader",
                    "sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "scopes": [SCOPE_QUERY],
                }
            ]
        ),
    )
    principal = authenticate_api_token(token)
    assert principal is not None
    assert principal.subject == "hashed-reader"
    assert principal.scopes == frozenset({SCOPE_QUERY})
    assert authenticate_api_token("wrong") is None


def test_invalid_scoped_key_registry_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATAPAW_API_TOKEN", raising=False)
    monkeypatch.setenv(API_KEYS_ENV, '{"not": "an array"}')
    with pytest.raises(ValueError, match=API_KEYS_ENV):
        install_api_token_auth(_build_app(), enforce_scopes=True)


def test_every_application_api_route_has_an_explicit_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATAPAW_API_TOKEN", raising=False)
    from context_manager.api.authorization import unclassified_app_routes
    from context_manager.api.server import create_app

    assert unclassified_app_routes(create_app()) == []
