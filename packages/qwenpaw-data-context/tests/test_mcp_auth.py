from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from context_manager.api.auth import API_KEYS_ENV, install_api_token_auth

from context_manager.mcp import cm_server
from context_manager.mcp.http_common import (
    _transport_security,
    create_streamable_mcp,
    is_loopback_bind_host,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost"])
def test_loopback_bind_hosts(host: str) -> None:
    assert is_loopback_bind_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "example.com"])
def test_external_bind_hosts(host: str) -> None:
    assert is_loopback_bind_host(host) is False


async def test_mcp_uses_native_token_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "mcp-secret")
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    server = create_streamable_mcp("auth-test")

    assert server.settings.auth is not None
    verifier = server._token_verifier
    assert verifier is not None
    assert await verifier.verify_token("wrong") is None
    access = await verifier.verify_token("mcp-secret")
    assert access is not None
    assert access.client_id == "legacy-admin"
    assert set(access.scopes) == {"query", "write", "manage", "credentials:manage"}


def test_mcp_auth_disabled_for_loopback_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    server = create_streamable_mcp("no-auth-test")
    assert server.settings.auth is None
    assert server._token_verifier is None


async def test_mcp_preserves_scoped_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.setenv(
        API_KEYS_ENV,
        json.dumps(
            [
                {
                    "id": "mcp-reader",
                    "token": "mcp-reader-token-123456",
                    "scopes": ["query"],
                }
            ]
        ),
    )
    server = create_streamable_mcp("scoped-auth-test")
    verifier = server._token_verifier
    assert verifier is not None
    access = await verifier.verify_token("mcp-reader-token-123456")
    assert access is not None
    assert access.client_id == "mcp-reader"
    assert access.scopes == ["query"]


async def test_embedded_mcp_client_uses_an_allowed_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    monkeypatch.delenv("QWENPAW_DATA_API_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv(
        "QWENPAW_DATA_SECURITY_AUDIT_LOG",
        str(tmp_path / "security-audit.jsonl"),
    )
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    install_api_token_auth(app)
    monkeypatch.setattr(cm_server.mcp, "_cm_app", app, raising=False)

    async with cm_server._async_client() as client:
        response = await client.get("/api/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_mcp_transport_uses_exact_origin_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.delenv("QWENPAW_DATA_MCP_ALLOWED_ORIGINS", raising=False)
    settings = _transport_security()
    assert settings.allowed_origins == ["http://localhost:3000"]

    monkeypatch.setenv("QWENPAW_DATA_MCP_ALLOWED_ORIGINS", "*")
    with pytest.raises(ValueError):
        _transport_security()


def test_standalone_mcp_reuses_api_edge_security(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "standalone-mcp-token")
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    monkeypatch.delenv("QWENPAW_DATA_API_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv(
        "QWENPAW_DATA_SECURITY_AUDIT_LOG",
        str(tmp_path / "security-audit.jsonl"),
    )
    client = TestClient(cm_server.standalone_http_app())

    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1"}).status_code == 401
