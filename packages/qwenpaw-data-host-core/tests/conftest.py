# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from qwenpaw_data.host.core.db.engine import TEST_TENANT_ID, TEST_USER_ID, TEST_WORKSPACE_ID
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import PreferencesRepository
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.workspace import stop_workspace_manager

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)

# Fixed 32-byte secret for tests (64 hex chars).
_TEST_PREFERENCES_MASTER_SECRET = "ab" * 32

SeedPreferences = Callable[..., Awaitable[None]]

# Ambient model credentials would turn rule-based turns into real LLM calls;
# ambient auth settings would change the local-mode gate under test.
_AMBIENT_ENV_VARS = (
    "QWENPAW_DATA_MODEL_PROVIDER",
    "QWENPAW_DATA_MODEL_NAME",
    "QWENPAW_DATA_MODEL_LIGHT_NAME",
    "QWENPAW_DATA_MODEL_API_KEY",
    "QWENPAW_DATA_MODEL_BASE_URL",
    "LLM_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "QWENPAW_DATA_API_TOKEN",
    "QWENPAW_DATA_AUTH_MODE",
    "QWENPAW_DATA_AUTH_URL",
    "QWENPAW_DATA_SERVICE_SECRET",
    "QWENPAW_DATA_ENV",
)

_TEST_CLIENT_INIT = TestClient.__init__


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str((tmp_path / "home").resolve()))
    # Entity-linking vocabulary fetches add network waits to every chat turn;
    # linking tests opt back in explicitly.
    monkeypatch.setenv("QWENPAW_DATA_BIZ_LINK_ENABLED", "0")


@pytest.fixture(autouse=True)
def _loopback_test_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare TestClient(app) acts as a same-machine browser in local auth mode."""

    def _init(self, app, base_url="http://localhost", *args, client=("127.0.0.1", 50000), **kwargs):
        _TEST_CLIENT_INIT(self, app, base_url, *args, client=client, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _init)


@pytest.fixture(autouse=True)
def _preferences_master_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "QWENPAW_DATA_PREFERENCES_MASTER_SECRET", _TEST_PREFERENCES_MASTER_SECRET
    )
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _host_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL",
        f"sqlite+aiosqlite:///{(tmp_path / 'host.sqlite3').resolve()}",
    )
    monkeypatch.setenv("QWENPAW_DATA_CHANNELS", "feishu,dingtalk,wecom,wechat")
    monkeypatch.setenv("QWENPAW_DATA_PROVIDERS", "dashscope,openai")
    clear_settings()


@pytest.fixture(autouse=True)
async def _stop_workspace_manager() -> None:
    await stop_workspace_manager()


async def _seed_test_preferences(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str = TEST_USER_ID,
    provider_id: str = "dashscope",
    model_id: str = "qwen-plus",
    light_model_id: str | None = "qwen3.7-flash",
    api_key: str = "test-secret",
) -> None:
    identity = Identity(
        user_id=user_id,
        tenant_id=TEST_TENANT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )
    async with session_factory() as db:
        repo = PreferencesRepository(db, identity)
        await repo.upsert_provider(
            provider_id,
            {"api_key": api_key, "base_url": None},
        )
        await repo.set_active_models(
            default_provider_id=provider_id,
            default_model_id=model_id,
            light_provider_id=provider_id if light_model_id else None,
            light_model_id=light_model_id,
        )
        await db.commit()


@pytest.fixture
def seed_test_preferences() -> SeedPreferences:
    return _seed_test_preferences
