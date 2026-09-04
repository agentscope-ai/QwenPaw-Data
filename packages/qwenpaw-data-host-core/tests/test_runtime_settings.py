# -*- coding: utf-8 -*-
"""Per-user runtime settings: store conformance and routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from qwenpaw_data.host.core.store.json_store import JSONPreferencesStore

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import SQLPreferencesStore  # noqa: E402


@pytest.fixture(params=["json", "sql"])
async def prefs(request, tmp_path: Path):
    if request.param == "json":
        yield JSONPreferencesStore(tmp_path)
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    await init_db(engine)
    yield SQLPreferencesStore(factory)
    await engine.dispose()


async def test_defaults_and_overrides(prefs, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_REACT_MAX_ITERS", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_LLM_RETRY_ENABLED", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_LLM_MAX_RETRIES", raising=False)

    initial = await prefs.get_runtime_settings("local")
    assert initial == {
        "react_max_iters": 10000,
        "llm_retry_enabled": True,
        "llm_max_retries": 3,
    }

    updated = await prefs.set_runtime_settings(
        "local", {"react_max_iters": 500, "llm_retry_enabled": False}
    )
    assert updated["react_max_iters"] == 500
    assert updated["llm_retry_enabled"] is False
    assert updated["llm_max_retries"] == 3  # untouched → default

    # Partial update keeps prior overrides; None clears back to defaults.
    again = await prefs.set_runtime_settings("local", {"llm_max_retries": 5})
    assert again["react_max_iters"] == 500
    assert again["llm_max_retries"] == 5
    cleared = await prefs.set_runtime_settings("local", {"react_max_iters": None})
    assert cleared["react_max_iters"] == 10000

    # Per-user isolation.
    other = await prefs.get_runtime_settings("someone-else")
    assert other["react_max_iters"] == 10000


async def test_env_defaults(prefs, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_REACT_MAX_ITERS", "42")
    monkeypatch.setenv("QWENPAW_DATA_LLM_RETRY_ENABLED", "off")
    assert await prefs.get_runtime_settings("local") == {
        "react_max_iters": 42,
        "llm_retry_enabled": False,
        "llm_max_retries": 3,
    }


# ---------------------------------------------------------------------------
# Routes

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch):
    for env in (
        "QWENPAW_DATA_API_TOKEN",
        "QWENPAW_DATA_DB_URL",
        "QWENPAW_DATA_STORE",
        "QWENPAW_DATA_REACT_MAX_ITERS",
        "QWENPAW_DATA_LLM_RETRY_ENABLED",
        "QWENPAW_DATA_LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(env, raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http


async def test_runtime_settings_routes(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as http:
        fetched = await http.get("/api/v1/preferences/runtime-settings")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["runtime_settings"]["react_max_iters"] == 10000

        saved = await http.put(
            "/api/v1/preferences/runtime-settings",
            json={"react_max_iters": 250, "llm_max_retries": 1},
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()["runtime_settings"]
        assert body["react_max_iters"] == 250
        assert body["llm_max_retries"] == 1

        again = await http.get("/api/v1/preferences/runtime-settings")
        assert again.json()["runtime_settings"]["react_max_iters"] == 250

        invalid = await http.put(
            "/api/v1/preferences/runtime-settings",
            json={"react_max_iters": "not-a-number"},
        )
        assert invalid.status_code == 422
