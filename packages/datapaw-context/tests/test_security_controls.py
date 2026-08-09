from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from context_manager.api.auth import install_api_token_auth
from context_manager.api.security import (
    SecurityControls,
    configured_api_hosts,
    configured_cors_origins,
)


def _build_security_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    origins: list[str] | None = None,
) -> FastAPI:
    monkeypatch.setenv("DATAPAW_API_TOKEN", "security-test-token")
    monkeypatch.delenv("DATAPAW_API_KEYS", raising=False)
    monkeypatch.setenv(
        "DATAPAW_SECURITY_AUDIT_LOG",
        str(tmp_path / "security-audit.jsonl"),
    )
    monkeypatch.setenv("DATAPAW_API_ALLOWED_HOSTS", "testserver")
    allowed = origins or ["http://localhost:3000"]
    app = FastAPI()

    @app.get("/api/v1/cm/domains")
    async def query_endpoint(request: Request) -> dict[str, str]:
        controls = request.app.state.security_controls
        return {"client_ip": controls.client_ip(request)}

    @app.post("/api/feedback")
    async def write_endpoint() -> dict[str, bool]:
        return {"written": True}

    install_api_token_auth(app, enforce_scopes=True, allowed_origins=allowed)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    return app


@pytest.mark.parametrize(
    "raw",
    ["*", "null", "http://localhost:3000/app", "https://user@example.com"],
)
def test_cors_allowlist_rejects_non_origins(raw: str) -> None:
    with pytest.raises(ValueError):
        configured_cors_origins(raw)


def test_cors_allowlist_normalizes_and_deduplicates() -> None:
    assert configured_cors_origins(
        "http://LOCALHOST:80/,http://localhost,http://127.0.0.1:3000"
    ) == ["http://localhost", "http://127.0.0.1:3000"]


def test_api_host_allowlist_is_explicit() -> None:
    assert configured_api_hosts("LOCALHOST,127.0.0.1,[::1]") == [
        "localhost",
        "127.0.0.1",
        "::1",
    ]
    with pytest.raises(ValueError):
        configured_api_hosts("*")


def test_default_api_hosts_include_docker_workspace_alias(monkeypatch) -> None:
    monkeypatch.delenv("DATAPAW_API_ALLOWED_HOSTS", raising=False)
    assert configured_api_hosts() == [
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
    ]


def test_origin_guard_allows_allowlisted_browser_and_non_browser_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    client = TestClient(_build_security_app(monkeypatch, tmp_path))
    auth = {"Authorization": "Bearer security-test-token"}

    allowed = client.post(
        "/api/feedback",
        headers={**auth, "Origin": "http://localhost:3000"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert client.post("/api/feedback", headers=auth).status_code == 200
    assert client.post(
        "/api/feedback",
        headers={**auth, "Origin": "http://testserver"},
    ).status_code == 200

    denied = client.post(
        "/api/feedback",
        headers={**auth, "Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    assert client.post(
        "/api/feedback",
        headers={**auth, "Sec-Fetch-Site": "cross-site"},
    ).status_code == 403
    assert client.get(
        "/api/v1/cm/domains",
        headers={**auth, "Host": "attacker.example"},
    ).status_code == 400


def test_authentication_failures_trigger_penalty_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DATAPAW_AUTH_FAILURE_LIMIT", "2")
    monkeypatch.setenv("DATAPAW_AUTH_FAILURE_WINDOW_SECONDS", "3600")
    client = TestClient(_build_security_app(monkeypatch, tmp_path))

    assert client.get("/api/v1/cm/domains").status_code == 401
    assert client.get(
        "/api/v1/cm/domains", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    blocked = client.get(
        "/api/v1/cm/domains",
        headers={"Authorization": "Bearer security-test-token"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_per_principal_scope_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DATAPAW_RATE_LIMIT_QUERY", "2")
    monkeypatch.setenv("DATAPAW_RATE_LIMIT_WINDOW_SECONDS", "3600")
    client = TestClient(_build_security_app(monkeypatch, tmp_path))
    auth = {"Authorization": "Bearer security-test-token"}

    first = client.get("/api/v1/cm/domains", headers=auth)
    second = client.get("/api/v1/cm/domains", headers=auth)
    limited = client.get("/api/v1/cm/domains", headers=auth)
    assert first.status_code == second.status_code == 200
    assert first.headers["RateLimit-Remaining"] == "1"
    assert second.headers["RateLimit-Remaining"] == "0"
    assert limited.status_code == 429
    assert limited.headers["RateLimit-Remaining"] == "0"


def test_forwarded_ip_is_used_only_for_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DATAPAW_TRUSTED_PROXIES", "10.0.0.0/8")
    controls = SecurityControls.from_env(["http://localhost:3000"])
    trusted_request = SimpleNamespace(
        client=SimpleNamespace(host="10.1.2.3"),
        headers={"x-forwarded-for": "203.0.113.9, 10.1.2.3"},
    )
    untrusted_request = SimpleNamespace(
        client=SimpleNamespace(host="192.0.2.2"),
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert controls.client_ip(trusted_request) == "203.0.113.9"
    assert controls.client_ip(untrusted_request) == "192.0.2.2"
    spoofed_chain = SimpleNamespace(
        client=SimpleNamespace(host="10.1.2.3"),
        headers={"x-forwarded-for": "198.51.100.77, 203.0.113.9"},
    )
    assert controls.client_ip(spoofed_chain) == "203.0.113.9"


def test_privileged_audit_is_structured_and_contains_no_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    audit_path = tmp_path / "security-audit.jsonl"
    client = TestClient(_build_security_app(monkeypatch, tmp_path))
    response = client.post(
        "/api/feedback",
        headers={"Authorization": "Bearer security-test-token"},
        json={"password": "request-body-secret"},
    )
    assert response.status_code == 200

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    privileged = [record for record in records if record["event"] == "privileged_request"]
    assert privileged[-1]["outcome"] == "success"
    assert privileged[-1]["path"] == "/api/feedback"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "security-test-token" not in audit_text
    assert "request-body-secret" not in audit_text
    if os.name != "nt":  # Windows has no POSIX permission bits
        assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
