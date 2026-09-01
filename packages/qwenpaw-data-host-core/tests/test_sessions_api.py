# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http


async def test_session_crud_flow(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as http:
        created = await http.post(
            "/api/v1/sessions",
            json={"title": "销售分析", "datasource_id": "ds1"},
        )
        assert created.status_code == 200
        session = created.json()["session"]
        assert session["title"] == "销售分析"
        assert session["status"] == "idle"
        assert session["chat_count"] == 0
        sid = session["id"]

        got = await http.get(f"/api/v1/sessions/{sid}")
        assert got.status_code == 200
        assert got.json()["session"]["id"] == sid

        renamed = await http.patch(
            f"/api/v1/sessions/{sid}", json={"title": "改名"}
        )
        assert renamed.json()["session"]["title"] == "改名"

        listing = await http.get("/api/v1/sessions")
        assert listing.json()["total"] == 1

        filtered = await http.get("/api/v1/sessions", params={"search_text": "改"})
        assert filtered.json()["total"] == 1
        empty = await http.get("/api/v1/sessions", params={"search_text": "无"})
        assert empty.json()["total"] == 0

        deleted = await http.delete(f"/api/v1/sessions/{sid}")
        assert deleted.status_code == 204
        assert (await http.get(f"/api/v1/sessions/{sid}")).status_code == 404
        assert (await http.get("/api/v1/sessions")).json()["total"] == 0


async def test_chat_requires_existing_session(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as http:
        response = await http.post(
            "/api/v1/sessions/ses_missing/chats", json={"text": "hi"}
        )
        assert response.status_code == 404


async def test_chat_listing_and_sequence(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as http:
        sid = (
            await http.post("/api/v1/sessions", json={})
        ).json()["session"]["id"]

        # A fake-agent-free create would start a real runtime; stop it right away.
        created = await http.post(
            f"/api/v1/sessions/{sid}/chats", json={"text": "turn 1"}
        )
        assert created.status_code == 200
        chat = created.json()["chat"]
        assert chat["sequence"] == 1

        await http.post(f"/api/v1/sessions/{sid}/chats/{chat['id']}/stop")

        listing = await http.get(f"/api/v1/sessions/{sid}/chats")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert [c["sequence"] for c in items] == [1]

        session = (await http.get(f"/api/v1/sessions/{sid}")).json()["session"]
        assert session["chat_count"] == 1
        assert (
            await http.get("/api/v1/sessions/ses_missing/chats")
        ).status_code == 404
