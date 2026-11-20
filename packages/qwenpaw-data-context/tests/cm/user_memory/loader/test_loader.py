"""Verify per-message revisions, terminal history and extraction context."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from context_manager.db import initialize_database
from context_manager.settings.db import DbSettings
from context_manager.user.loader import (
    ChatLoader,
    ChatLoaderCheckpointRepository,
    UserHistoryScope,
)

SCOPE = UserHistoryScope("tenant-1", "workspace-1", "user-1")


def _history_context_tables(connection) -> None:
    """Add the Host metadata subset used to qualify user evidence."""
    connection.execute("ALTER TABLE chats ADD COLUMN datasource_id TEXT")
    connection.execute(
        "CREATE TABLE datasource (datasource_id TEXT, datasource_type TEXT, tenant_id TEXT, workspace_id TEXT, is_deleted INTEGER)"
    )
    connection.execute(
        "CREATE TABLE chat_events (chat_id TEXT, session_id TEXT, tenant_id TEXT, workspace_id TEXT, user_id TEXT, sequence_number INTEGER, object TEXT, payload TEXT, created_at TEXT)"
    )


def _history_database(path: Path) -> str:
    """Create the session-history table subset required by the loader."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE chats (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                user_input TEXT NOT NULL,
                status TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO sessions VALUES (
                'session-1', 'tenant-1', 'workspace-1', 'user-1', NULL
            );
            INSERT INTO sessions VALUES (
                'session-deleted', 'tenant-1', 'workspace-1', 'user-1',
                '2026-08-30T00:00:00Z'
            );
            INSERT INTO chats VALUES (
                'chat-1', 'session-1', 'tenant-1', 'workspace-1', 'user-1', 1,
                '  Remember revenue uses paid orders.  ', 'completed',
                '2026-08-31T14:00:00Z', '2026-08-31T14:00:00Z'
            );
            INSERT INTO chats VALUES (
                'chat-future', 'session-1', 'tenant-1', 'workspace-1', 'user-1', 2,
                'Future input', 'completed',
                '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z'
            );
            INSERT INTO chats VALUES (
                'chat-running', 'session-1', 'tenant-1', 'workspace-1', 'user-1', 3,
                'Incomplete input', 'running', NULL, '2026-08-31T14:00:00Z'
            );
            INSERT INTO chats VALUES (
                'chat-deleted', 'session-deleted', 'tenant-1', 'workspace-1',
                'user-1', 1, 'Deleted session', 'completed',
                '2026-08-31T14:00:00Z', '2026-08-31T14:00:00Z'
            );
            """
        )
        connection.executescript(
            "ALTER TABLE sessions ADD COLUMN created_at TEXT DEFAULT '2026-08-01T00:00:00Z';"
            "ALTER TABLE chats ADD COLUMN created_at TEXT;"
            "UPDATE chats SET created_at = updated_at;"
        )
        _history_context_tables(connection)
    dsn = f"sqlite:///{path}"
    initialize_database(DbSettings(url=dsn))
    return dsn


def test_loader_returns_only_pending_completed_scoped_inputs(
    tmp_path: Path,
) -> None:
    """The loader filters rows, groups a User, and owns acknowledgement."""
    _dsn = _history_database(tmp_path / "history.sqlite3")
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)
    cutoff = datetime(2026, 8, 31, 23, tzinfo=UTC)

    batches = loader.load_pending(cutoff_at=cutoff).batches

    assert len(batches) == 1
    assert batches[0].scope == UserHistoryScope(
        "tenant-1",
        "workspace-1",
        "user-1",
    )
    assert len(batches[0].messages) == 1
    assert batches[0].messages[0].message_id == "host-chat:chat-1:user"
    assert batches[0].messages[0].text == "Remember revenue uses paid orders."

    message = batches[0].messages[0]
    assert message.source_id == "message:tenant-1:workspace-1:user-1:session-1:chat-1"
    version = json.dumps(
        {"id": "chat-1", "user_input": "  Remember revenue uses paid orders.  "},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert message.source_version == sha256(version.encode()).hexdigest()
    loader.acknowledge(batches[0].scope, message, processed_at=cutoff)
    assert loader.is_processed(batches[0].scope, message)

    assert loader.load_pending(cutoff_at=cutoff).batches == ()

    # A new service instance sees the same committed consumption position.
    restarted = ChatLoader(ChatLoaderCheckpointRepository())
    assert restarted.load_pending(cutoff_at=cutoff).batches == ()


def test_edits_replay_but_timestamp_and_status_changes_do_not(tmp_path):
    path = tmp_path / "history.sqlite3"
    _dsn = _history_database(path)
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    cutoff = datetime(2026, 8, 31, 23, tzinfo=UTC)
    original = loader.load_pending(cutoff_at=cutoff).batches[0].messages[0]
    loader.acknowledge(SCOPE, original, processed_at=cutoff)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE chats SET status='failed', updated_at='2026-08-31T15:00:00Z' WHERE id='chat-1'"
        )
    assert loader.load_pending(cutoff_at=cutoff).batches == ()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chats SET user_input='Use net revenue.' WHERE id='chat-1'")
    edited = loader.load_pending(cutoff_at=cutoff).batches[0].messages[0]
    assert not loader.is_processed(SCOPE, edited)
    assert edited.source_id == original.source_id
    assert edited.source_version != original.source_version
    # Acknowledging the loaded old version cannot accidentally consume a later edit.
    loader.acknowledge(SCOPE, original, processed_at=cutoff)
    assert loader.load_pending(cutoff_at=cutoff).batches[0].messages[0] == edited
    loader.acknowledge(SCOPE, edited, processed_at=cutoff)
    assert loader.load_pending(cutoff_at=cutoff).batches == ()


def test_late_messages_and_updater_changes_remain_pending(tmp_path):
    path = tmp_path / "history.sqlite3"
    _dsn = _history_database(path)
    store = ChatLoaderCheckpointRepository()
    loader = ChatLoader(store)
    cutoff = datetime(2026, 8, 31, 23, tzinfo=UTC)
    message = loader.load_pending(cutoff_at=cutoff).batches[0].messages[0]
    loader.acknowledge(SCOPE, message, processed_at=cutoff)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE chats SET status='completed', sequence=0, completed_at='2026-08-31T13:00:00Z', created_at='2026-08-31T13:00:00Z' WHERE id='chat-running'"
        )
    pending = loader.load_pending(cutoff_at=cutoff).batches[0].messages
    assert [item.message_id for item in pending] == ["host-chat:chat-running:user"]
    loader.acknowledge(SCOPE, pending[0], processed_at=cutoff)
    upgraded = ChatLoader(store, updater_version="next-extractor")
    assert len(upgraded.load_pending(cutoff_at=cutoff).batches[0].messages) == 2


def test_context_is_complete_and_scoped_to_the_same_session(tmp_path):
    path = tmp_path / "history.sqlite3"
    _dsn = _history_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM chats")
        conn.execute(
            "INSERT INTO sessions VALUES ('session-2', 'tenant-1', 'workspace-1', 'user-1', NULL, '2026-08-01T00:00:00Z')"
        )
        for index in range(61):
            conn.execute(
                "INSERT INTO chats VALUES (?, ?, 'tenant-1', 'workspace-1', 'user-1', ?, 'Keep it concise.', 'completed', ?, ?, ?, NULL)",
                (
                    f"c{index:03d}",
                    "session-1" if index < 60 else "session-2",
                    index,
                    *("2026-08-31T14:00:00Z",) * 3,
                ),
            )
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    cutoff = datetime(2026, 8, 31, 23, tzinfo=UTC)
    loaded = loader.load_pending(cutoff_at=cutoff)
    messages = loaded.batches[0].messages
    assert len(messages[59].context) == 59
    assert messages[59].context[0]["id"] == "host-chat:c000:user"
    assert messages[60].context == ()
    assert len(loaded.evidence_times[SCOPE]) == 61
    loader.acknowledge(SCOPE, messages[0], processed_at=cutoff)
    refreshed = loader.load_pending(cutoff_at=cutoff)
    assert messages[0] not in refreshed.batches[0].messages
    assert refreshed.evidence_times[SCOPE] == loaded.evidence_times[SCOPE]
