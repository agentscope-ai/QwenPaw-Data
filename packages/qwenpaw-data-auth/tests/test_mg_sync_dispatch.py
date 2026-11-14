from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw_data_auth.asgi_dispatch import DualASGI, is_auth_path
from qwenpaw_data_auth.mg_sync import sync_user_workspaces
from qwenpaw_data_auth.token import TokenVerifier
from conftest import seed_user


@pytest.mark.parametrize("path,expected", [("/api/auth/status", True), ("/api/platform", True), ("/api/users", True),
                                          ("/api/userspace", False), ("/api/workspaces-other", False), ("/api/v1/mg", False)])
def test_auth_dispatch_path_boundaries(path, expected):
    assert is_auth_path(path) == expected


def test_dual_asgi_runs_both_lifespans_and_dispatches_intact_paths():
    events = []

    def make_app(label):
        @asynccontextmanager
        async def lifespan(app):
            events.append(label + " start")
            yield
            events.append(label + " stop")

        app = FastAPI(lifespan=lifespan)

        @app.get("/{path:path}")
        async def route(path):
            return {"app": label, "path": path}
        return app

    with TestClient(DualASGI(make_app("auth"), make_app("context"))) as client:
        assert client.get("/api/auth/status").json() == {"app": "auth", "path": "api/auth/status"}
        assert client.get("/api/userspace").json()["app"] == "context"
    assert events == ["auth start", "context start", "context stop", "auth stop"]


def test_mg_sync_local_without_external_auth_url_and_independent_failures(multi_client, monkeypatch):
    cfg = multi_client.app.state.auth_config
    seed_user(cfg, role="tenant_admin")
    from qwenpaw_data_auth.database import get_db
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute("INSERT INTO workspaces (id, tenant_id, name) VALUES ('w2', 't1', 'Second')")
        conn.commit()
    calls = []
    real_client = httpx.AsyncClient

    def handler(request):
        claims = TokenVerifier(cfg.secret_keys).verify(request.headers["authorization"][7:])
        calls.append(claims)
        return httpx.Response(503 if claims["active_ws"] == "w1" else 200, json={"ok": True})

    monkeypatch.setattr("qwenpaw_data_auth.mg_sync.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    from dataclasses import replace
    asyncio.run(sync_user_workspaces(replace(cfg, cm_base_url="http://context.example"), "t1", "u1", {"w1", "w2"}))
    assert [c["active_ws"] for c in calls] == ["w1", "w2"]
    assert all(c["role"] == "tenant_admin" for c in calls)


def test_mg_sync_uses_remote_service_credential_when_configured(multi_client, monkeypatch):
    from dataclasses import replace
    cfg = replace(multi_client.app.state.auth_config, auth_url="http://auth.example", cm_base_url="http://context.example")
    calls = []
    real_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        if request.url.host == "auth.example":
            assert request.headers["x-qwenpaw-data-service-secret"] == cfg.service_secret
            return httpx.Response(200, json={"access_token": "co-signed-token"})
        assert request.headers["authorization"] == "Bearer co-signed-token"
        return httpx.Response(200, json={})

    monkeypatch.setattr("qwenpaw_data_auth.mg_sync.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(sync_user_workspaces(cfg, "t1", "u1", {"w1"}))
    assert len(calls) == 2
