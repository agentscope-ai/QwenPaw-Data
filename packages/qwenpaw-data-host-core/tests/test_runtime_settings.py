"""Runtime settings through the service SQL repository and authenticated API."""

from __future__ import annotations

import os
from dataclasses import replace

import httpx
import pytest

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import get_session_factory, init_db, reset_engine
from qwenpaw_data.host.core.repositories import PreferencesRepository
from qwenpaw_data.host.core.settings import clear_settings

from .conftest import TEST_IDENTITY


@pytest.fixture(autouse=True)
def runtime_defaults(monkeypatch):
    for key in list(os.environ):
        if key.startswith("QWENPAW_DATA_RUNTIME_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("QWENPAW_DATA_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_TOOL_CONFIRMATION_DEFAULT_LEVEL", "auto")
    clear_settings()


@pytest.fixture
async def database():
    await reset_engine()
    await init_db()
    try:
        yield get_session_factory()
    finally:
        await reset_engine()


@pytest.fixture
async def service():
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            trust_env=False,
        ) as http:
            yield http


async def test_defaults_and_overrides(database) -> None:
    async with database() as db:
        prefs = PreferencesRepository(db, TEST_IDENTITY)
        initial = await prefs.get_runtime_settings()
        assert initial == {
            "react_max_iters": 10000,
            "llm_retry_enabled": True,
            "llm_max_retries": 3,
            "context_trigger_ratio": 0.8,
            "context_reserve_ratio": 0.1,
            "tool_result_limit": 50000,
            "tool_timeout_enabled": True,
            "tool_timeout_seconds": 1800,
            "tool_confirmation_level": "auto",
        }
        updated = await prefs.set_runtime_settings(
            {"react_max_iters": 500, "llm_retry_enabled": False}
        )
        assert updated == {
            **initial,
            "react_max_iters": 500,
            "llm_retry_enabled": False,
        }
        await db.commit()

    # Reload from a different SQL session, rather than an in-memory identity map.
    async with database() as db:
        prefs = PreferencesRepository(db, TEST_IDENTITY)
        assert await prefs.get_runtime_settings() == updated
        again = await prefs.set_runtime_settings({"llm_max_retries": 5})
        assert again == {**updated, "llm_max_retries": 5}
        cleared = await prefs.set_runtime_settings({"react_max_iters": None})
        assert cleared == {**again, "react_max_iters": 10000}
        await db.commit()

    async with database() as db:
        other = PreferencesRepository(
            db, replace(TEST_IDENTITY, user_id="someone-else")
        )
        assert await other.get_runtime_settings() == initial
        original = PreferencesRepository(db, TEST_IDENTITY)
        assert await original.get_runtime_settings() == cleared


async def test_env_defaults_and_persisted_override(database, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_REACT_MAX_ITERS", "42")
    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_LLM_RETRY_ENABLED", "false")
    clear_settings()
    async with database() as db:
        prefs = PreferencesRepository(db, TEST_IDENTITY)
        settings = await prefs.get_runtime_settings()
        assert settings["react_max_iters"] == 42
        assert settings["llm_retry_enabled"] is False
        assert settings["llm_max_retries"] == 3
        await prefs.set_runtime_settings({"react_max_iters": 7})
        await db.commit()

    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_REACT_MAX_ITERS", "99")
    clear_settings()
    async with database() as db:
        prefs = PreferencesRepository(db, TEST_IDENTITY)
        assert (await prefs.get_runtime_settings())["react_max_iters"] == 7
        assert (await prefs.set_runtime_settings({"react_max_iters": None}))[
            "react_max_iters"
        ] == 99


async def test_runtime_settings_routes(service) -> None:
    path = "/api/preferences/runtime-settings"
    fetched = await service.get(path)
    assert fetched.status_code == 200, fetched.text
    initial = fetched.json()["runtime_settings"]
    assert initial["react_max_iters"] == 10000

    saved = await service.put(path, json={"react_max_iters": 250, "llm_max_retries": 1})
    assert saved.status_code == 200, saved.text
    expected = {**initial, "react_max_iters": 250, "llm_max_retries": 1}
    assert saved.json()["runtime_settings"] == expected
    assert (await service.get(path)).json()["runtime_settings"] == expected

    invalid = await service.put(path, json={"react_max_iters": "not-a-number"})
    assert invalid.status_code == 422
    assert (await service.get(path)).json()["runtime_settings"] == expected

    # Invalid merged compression bounds must not partially persist other edits.
    invalid_bounds = await service.put(
        path, json={"react_max_iters": 999, "context_reserve_ratio": 0.85}
    )
    assert invalid_bounds.status_code == 400, invalid_bounds.text
    assert (await service.get(path)).json()["runtime_settings"] == expected
