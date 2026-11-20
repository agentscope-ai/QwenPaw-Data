"""Verify develop's Host evidence and per-session isolation policies."""

import sqlite3
from datetime import UTC, datetime

import pytest
from context_manager.user.loader import ChatLoader, ChatLoaderCheckpointRepository
from context_manager.user.loader.database import history_connection
from sqlalchemy.exc import OperationalError

from cm.user_memory.loader.test_loader import SCOPE, _history_database

CUTOFF = datetime(2026, 8, 31, 23, tzinfo=UTC)


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "history.sqlite3"
    _dsn = _history_database(path)
    return path, ChatLoader(ChatLoaderCheckpointRepository())


def _healthy_peer(connection):
    connection.execute(
        "INSERT INTO sessions VALUES ('peer', 'tenant-1', 'workspace-1', 'user-1', NULL, '2026-08-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO chats SELECT 'peer-chat', 'peer', tenant_id, workspace_id, user_id, 0, 'Keep concise answers.', status, completed_at, updated_at, created_at, datasource_id FROM chats WHERE id='chat-1'"
    )


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (
            "UPDATE chats SET user_id='another-user' WHERE id='chat-running'",
            "chat_scope_mismatch",
        ),
        (
            "UPDATE chats SET sequence=1 WHERE id='chat-running'",
            "invalid_chat_sequence",
        ),
        (
            "UPDATE chats SET sequence=-1 WHERE id='chat-running'",
            "invalid_chat_sequence",
        ),
        (
            "UPDATE chats SET created_at='broken' WHERE id='chat-running'",
            "invalid_source_timestamp",
        ),
        (
            "UPDATE chats SET completed_at=NULL WHERE id='chat-1'",
            "invalid_source_timestamp",
        ),
        (
            "UPDATE sessions SET created_at='broken' WHERE id='session-1'",
            "invalid_source_timestamp",
        ),
    ],
)
def test_bad_session_is_reported_without_discarding_healthy_peers(
    history, statement, reason
):
    path, loader = history
    with sqlite3.connect(path) as connection:
        _healthy_peer(connection)
        connection.execute(statement)
    result = loader.load_pending(cutoff_at=CUTOFF, target_scope=SCOPE)
    assert [message.message_id for message in result.batches[0].messages] == [
        "host-chat:peer-chat:user"
    ]
    assert len(result.issues) == 1
    assert result.issues[0]["reason"] == reason
    assert result.issues[0]["file"] == "session-1"


def test_history_database_is_read_only_and_missing_files_remain_missing(tmp_path):
    path = tmp_path / "not-started.sqlite3"
    _dsn = f"sqlite+aiosqlite:///{path}"
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    with pytest.raises(RuntimeError, match="has not been initialized"):
        loader.load_pending(cutoff_at=CUTOFF)
    assert not path.exists()
    _history_database(path)
    with (
        history_connection() as connection,
        pytest.raises(OperationalError, match="readonly"),
    ):
        connection.connection.exec_driver_sql("DELETE FROM chats")
    assert len(loader.load_pending(cutoff_at=CUTOFF).batches) == 1
