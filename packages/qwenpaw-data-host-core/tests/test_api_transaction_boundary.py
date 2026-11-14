"""HTTP success must not outrun the SQL commit it acknowledges."""

from __future__ import annotations

import inspect
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI
from fastapi.params import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api import routers
from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.routers import preferences, sessions
from qwenpaw_data.host.core.db.tables import SessionRow
from qwenpaw_data.host.core.repositories import PreferencesRepository

from . import test_runtime_settings as settings_tests
from .conftest import TEST_IDENTITY

database = settings_tests.database


def _app(prefix="/api"):
    app = FastAPI()
    app.include_router(sessions.router, prefix=prefix)
    app.include_router(preferences.router, prefix=prefix)
    app.dependency_overrides[get_identity] = lambda: TEST_IDENTITY
    return app


def test_all_route_transactions_close_before_response():
    checked = 0
    for module in vars(routers).values():
        if not isinstance(module, ModuleType):
            continue
        for name, function in inspect.getmembers(module, inspect.iscoroutinefunction):
            if function.__module__ != module.__name__:
                continue
            for parameter in inspect.signature(function).parameters.values():
                dependency = parameter.default
                if isinstance(dependency, Depends) and dependency.dependency is get_db:
                    assert dependency.scope == "function", f"{module.__name__}.{name}"
                    checked += 1
    assert checked > 0


@pytest.mark.parametrize("resource", ["session", "preferences"])
@pytest.mark.parametrize("prefix", ["/api", "/api/v1"])
async def test_write_is_visible_before_success_headers(database, resource, prefix):
    app = _app(prefix)
    visible_at_headers = []

    async def observe(scope, receive, send):
        async def checked_send(message):
            if message["type"] == "http.response.start" and message["status"] == 200:
                # A second connection models an immediate follow-up request.
                # ASGITransport alone waits for dependency cleanup and hides this race.
                async with database() as reader:
                    if resource == "session":
                        visible = await reader.scalar(
                            select(func.count()).select_from(SessionRow)
                        )
                    else:
                        prefs = await PreferencesRepository(
                            reader, TEST_IDENTITY
                        ).get_runtime_settings()
                        visible = prefs["react_max_iters"] == 123
                    visible_at_headers.append(bool(visible))
            await send(message)

        await app(scope, receive, checked_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observe), base_url="http://test"
    ) as client:
        if resource == "session":
            response = await client.post(
                f"{prefix}/sessions", json={"title": "committed"}
            )
        else:
            response = await client.put(
                f"{prefix}/preferences/runtime-settings", json={"react_max_iters": 123}
            )
    assert response.status_code == 200, response.text
    assert visible_at_headers == [True]


async def test_commit_failure_never_acknowledges_success(database, monkeypatch):
    async def fail_commit(self):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/sessions", json={"title": "must roll back"})
    assert response.status_code == 500
    async with database() as reader:
        assert await reader.scalar(select(func.count()).select_from(SessionRow)) == 0
