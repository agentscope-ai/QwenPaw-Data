"""Isolate process settings and SQL/search-session storage between tests."""

import pytest
from context_manager.db import close_database
from context_manager.settings import clear_settings
from sqlalchemy import create_engine


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("DATAAGENT_SESSIONS_DB", str(tmp_path / "search-sessions.db"))
    # In-process HTTP clients address the MCP transport by these test host names.
    monkeypatch.setenv("QWENPAW_DATA_MCP_ALLOWED_HOSTS", "testserver,cm")
    # Runtime workers need independent SQLite connections and transactions.
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"
    )
    clear_settings()
    yield
    close_database()
    clear_settings()


@pytest.fixture
def database_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coordination.db'}")
    yield engine
    engine.dispose()
