"""Verify shared persistence of exact message and extraction versions."""

from dataclasses import replace
from datetime import UTC, datetime

from context_manager.db import initialize_database
from context_manager.settings.db import DbSettings
from context_manager.user.loader import (
    ChatHistoryMessage,
    ChatLoaderCheckpointRepository,
    UserHistoryScope,
)
from context_manager.user.loader import repository as cp
from sqlalchemy import select

_NOW = datetime(2026, 9, 29, 14, tzinfo=UTC)
_SCOPE = UserHistoryScope("tenant-1", "workspace-1", "user-1")
_VERSION = "test-v1"
_MESSAGE = ChatHistoryMessage("chat:1", "Remember this.", _NOW, "message:1", "hash-1")


def _snapshot(store, scope=_SCOPE, version=_VERSION):
    return store.snapshot(scope, updater_version=version)


def _mark(store, message=_MESSAGE, scope=_SCOPE, version=_VERSION):
    store.mark_processed(scope, message, updater_version=version, processed_at=_NOW)


def test_message_and_updater_versions_replace_only_the_same_source(tmp_path):
    """Both version dimensions participate in repeat-processing decisions."""
    dsn = f"sqlite:///{tmp_path / 'host.sqlite3'}"
    initialize_database(DbSettings(url=dsn))
    store = ChatLoaderCheckpointRepository()
    _mark(store)
    _mark(store)
    _mark(store, replace(_MESSAGE, source_id="message:2"))
    _mark(store, replace(_MESSAGE, source_version="hash-edited"))
    assert _snapshot(store) == {"message:1": "hash-edited", "message:2": "hash-1"}
    assert _snapshot(store, version="test-v2") == {}
    _mark(store, replace(_MESSAGE, source_version="hash-edited"), version="test-v2")
    assert _snapshot(store, version="test-v2") == {"message:1": "hash-edited"}
    assert _snapshot(store) == {"message:2": "hash-1"}
    with store._engine.connect() as connection:
        assert len(connection.execute(select(cp._checkpoints)).all()) == 2
