"""Semantic-layer writes must be committed before the response is sent (walkthrough 404 regression)."""

from __future__ import annotations

import aiosqlite
import httpx
from fastapi import FastAPI

from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.errors import register_exception_handlers
from semantic_config.routers import datasource_router


_PAYLOAD = {
    "datasource_name": "holo业务库",
    "datasource_type": "postgresql",
    "config": {
        "host": "holo.example.com",
        "port": 8099,
        "database": "busi",
        "user": "u",
        "password": "p",
    },
}


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(datasource_router.router)
    return app


async def _count_rows(db_path: str) -> int:
    """Count rows on an independent connection — simulating the one the "next request" gets."""
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT COUNT(1) FROM datasource") as cur:
            row = await cur.fetchone()
    return int(row[0])


async def test_datasource_is_committed_before_response_is_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)
    db_path = str(tmp_path / "semantic_config.db")
    monkeypatch.setattr(get_settings(), "db_url", "sqlite+aiosqlite:///" + db_path)

    await init_db()

    app = _app()
    seen: dict[str, int] = {}

    async def probe(scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start" and "rows" not in seen:
                seen["rows"] = await _count_rows(db_path)
            await send(message)

        await app(scope, receive, send_wrapper)

    transport = httpx.ASGITransport(app=probe)
    async with httpx.AsyncClient(transport=transport, base_url="http://sc") as client:
        created = await client.post("/api/semantic-config/datasource", json=_PAYLOAD)
        assert created.status_code == 200, created.text
        datasource_id = created.json()["datasource_id"]

        # The immediately-following request (another connection) must see the row — the walkthrough script hit a 404 here
        fetched = await client.get(f"/api/semantic-config/datasource/{datasource_id}")

    assert seen["rows"] == 1, "row must be committed before http.response.start"
    assert fetched.status_code == 200, fetched.text


async def test_failed_write_is_rolled_back(tmp_path, monkeypatch):
    """A failed request must leave no partial state: invalid type -> 400, and no new row in the table."""
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)
    db_path = str(tmp_path / "semantic_config.db")
    monkeypatch.setattr(get_settings(), "db_url", "sqlite+aiosqlite:///" + db_path)

    await init_db()

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sc") as client:
        rejected = await client.post(
            "/api/semantic-config/datasource",
            json={**_PAYLOAD, "datasource_type": "oracle"},
        )

    assert rejected.status_code == 400, rejected.text
    assert await _count_rows(db_path) == 0
