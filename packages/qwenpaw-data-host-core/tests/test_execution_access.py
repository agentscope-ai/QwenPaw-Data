# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import httpx
import pytest

from qwenpaw_data_auth import TokenIssuer, TokenVerifier
from qwenpaw_data.host.core.utils.auth import generate_temporary_token
from qwenpaw_data.host.core.domain.identity import Identity


def test_generate_temporary_token_returns_none_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)
    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    assert generate_temporary_token(identity) is None


def test_generate_temporary_token_calls_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "service-secret")
    issued = TokenIssuer(secret="test-secret").issue_access(
        "u1", "t1", "ws1", "member", 1800
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/temporary-access"
        assert request.headers["X-QwenPaw-Data-Service-Secret"] == "service-secret"
        return httpx.Response(200, json={"access_token": issued})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.utils.auth.httpx.post",
        lambda url, **kwargs: httpx.Client(transport=transport).post(url, **kwargs),
    )

    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    token = generate_temporary_token(identity, ttl=1800)
    assert token == issued
    payload = TokenVerifier(secrets=["test-secret"]).verify(token)
    assert payload["role"] == "member"


def test_generate_temporary_token_omits_ttl_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ttl is sent unless asked for, so Auth applies its own access_token_ttl."""
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "service-secret")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "tok"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.utils.auth.httpx.post",
        lambda url, **kwargs: httpx.Client(transport=transport).post(url, **kwargs),
    )

    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    assert generate_temporary_token(identity) == "tok"
    assert "ttl" not in seen[0]
    assert seen[0] == {"user_id": "u1", "tenant_id": "t1", "workspace_id": "ws1"}

    assert generate_temporary_token(identity, ttl=900) == "tok"
    assert seen[1]["ttl"] == 900


def test_generate_temporary_token_rejects_a_non_positive_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "service-secret")
    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    with pytest.raises(ValueError, match="ttl must be positive"):
        generate_temporary_token(identity, ttl=0)


def test_generate_temporary_token_maps_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "service-secret")
    monkeypatch.setattr(
        "qwenpaw_data.host.core.utils.auth.httpx.post",
        lambda *a, **k: httpx.Response(403, json={"detail": "no access"}),
    )
    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    with pytest.raises(PermissionError, match="no access"):
        generate_temporary_token(identity)


def test_generate_temporary_token_requires_auth_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_SERVICE_SECRET", "service-secret")
    monkeypatch.delenv("QWENPAW_DATA_AUTH_URL", raising=False)
    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="ws1")
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_AUTH_URL"):
        generate_temporary_token(identity)


def test_validate_startup_requires_service_secret_when_auth_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw_data.host.core.api.auth_middleware import validate_startup_configuration

    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_URL", "http://auth.test")
    monkeypatch.delenv("QWENPAW_DATA_SERVICE_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_SERVICE_SECRET"):
        validate_startup_configuration()
