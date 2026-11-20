# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402

LOOPBACK = ("127.0.0.1", 1234)
REMOTE = ("203.0.113.5", 4711)


@asynccontextmanager
async def service_client(tmp_path: Path, *, client=LOOPBACK):
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as http:
            yield http


async def test_no_token_allows_loopback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    async with service_client(tmp_path) as http:
        response = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
        )
        assert response.status_code == 404  # authenticated, chat not found


async def test_no_token_rejects_remote_clients(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    async with service_client(tmp_path, client=REMOTE) as http:
        response = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Local access requires a safe loopback client"}
        # Health stays reachable.
        assert (await http.get("/health")).status_code == 200


async def test_token_required_when_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "secret-token")
    async with service_client(tmp_path) as http:
        missing = await http.post("/api/v1/sessions/s1/chats/chat_missing/stop")
        assert missing.status_code == 401
        assert missing.headers["WWW-Authenticate"] == "Bearer"

        wrong = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong.status_code == 401
        assert wrong.headers["WWW-Authenticate"] == "Bearer"

        basic = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
            headers={"Authorization": "Basic secret-token"},
        )
        assert basic.status_code == 401
        assert basic.headers["WWW-Authenticate"] == "Bearer"

        ok = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert ok.status_code == 404  # authenticated, chat not found

        assert (await http.get("/health")).status_code == 200


async def test_token_authenticates_remote_clients(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "secret-token")
    async with service_client(tmp_path, client=REMOTE) as http:
        ok = await http.post(
            "/api/v1/sessions/s1/chats/chat_missing/stop",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert ok.status_code == 404


@pytest.mark.parametrize("detail", ["authentication required", {"code": "UNAUTHORIZED"}])
async def test_exception_translation_preserves_authentication_challenge(detail):
    from fastapi import HTTPException
    from qwenpaw_data.host.core.api.errors import http_exception_handler

    response = await http_exception_handler(
        None, HTTPException(401, detail=detail, headers={"WWW-Authenticate": "Bearer"})
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
