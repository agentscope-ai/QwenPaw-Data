"""Verify independently acknowledged message revisions and user isolation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from context_manager.db import initialize_database
from context_manager.runtime import create_context_runtime
from context_manager.settings import get_settings
from context_manager.settings.db import DbSettings
from context_manager.user import MemoryIngestService
from context_manager.user.loader import (
    ChatLoader,
    ChatLoaderCheckpointRepository,
    UserHistoryScope,
)
from context_manager.user.repository import UserIngestRepository
from qwenpaw_data_auth.database import get_db, init_schema

from cm.infrastructure.lifecycle.test_startup import (
    CapturingDriverFactory,
    _settings,
)
from cm.infrastructure.models.support import StaticModelService
from cm.user_memory.loader.test_loader import _history_context_tables
from cm.user_memory.support import FakeLlmCaller, history_adapter


@pytest.mark.asyncio
async def test_lost_lease_cancels_extraction_and_keeps_message_pending(
    tmp_path, monkeypatch
):
    _seed_unified_database(tmp_path / "lost.db")
    monkeypatch.setattr(get_settings().heartbeat, "interval_seconds", 0.1)
    runtime = create_context_runtime(
        _settings(), driver_factory=CapturingDriverFactory()
    )
    monkeypatch.setattr(
        runtime, "models", lambda _: StaticModelService(FakeLlmCaller())
    )
    monkeypatch.setattr(UserIngestRepository, "renew", lambda *_: False)
    cancelled = asyncio.Event()

    async def ingest(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(MemoryIngestService, "ingest", ingest)
    scope = UserHistoryScope("tenant-1", "workspace-1", "user-1")
    cutoff = datetime(2026, 8, 31, 15, tzinfo=UTC)
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    try:
        result = await asyncio.wait_for(
            history_adapter(runtime).ingest(cutoff, scope), timeout=5
        )
        assert cancelled.is_set()
        assert result["messages_failed"] == 1
        assert result["details"][0]["reason"] == "ingest_lease_lost"
        assert (
            len(loader.load_pending(cutoff_at=cutoff, target_scope=scope).batches) == 1
        )
        assert UserIngestRepository().claim(scope)
    finally:
        await runtime.aclose()


def _seed_unified_database(path: Path) -> str:
    """Create two active members and their completed chat-history inputs."""
    dsn = f"sqlite:///{path}"
    init_schema(dsn)
    with get_db(dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES (?, ?, ?)",
            ("tenant-1", "Tenant 1", "tenant_1"),
        )
        connection.execute(
            """
            INSERT INTO workspaces (
                id, tenant_id, name, status, instance_status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("workspace-1", "tenant-1", "Workspace 1", "active", "ready"),
        )
        for user_id in ("user-1", "user-2"):
            connection.execute(
                "INSERT INTO users (id, tenant_id, username) VALUES (?, ?, ?)",
                (user_id, "tenant-1", user_id),
            )
            connection.execute(
                """
                INSERT INTO role_bindings (
                    id, user_id, role, scope_type, scope_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (f"binding-{user_id}", user_id, "member", "workspace", "workspace-1"),
            )
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
        connection.execute(
            """
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
            )
            """
        )
        for index, user_id in enumerate(("user-1", "user-2"), start=1):
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)",
                (f"session-{index}", "tenant-1", "workspace-1", user_id),
            )
            connection.execute(
                """
                INSERT INTO chats VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?
                )
                """,
                (
                    f"chat-{index}",
                    f"session-{index}",
                    "tenant-1",
                    "workspace-1",
                    user_id,
                    1,
                    f"Remember preference {index}.",
                    "2026-08-31T14:00:00Z",
                    "2026-08-31T14:00:00Z",
                ),
            )
        connection.commit()
        connection.execute(
            "ALTER TABLE sessions ADD COLUMN created_at TEXT DEFAULT '2026-08-01T00:00:00Z'"
        )
        connection.execute("ALTER TABLE chats ADD COLUMN created_at TEXT")
        connection.execute("UPDATE chats SET created_at=updated_at")
        _history_context_tables(connection)
        connection.commit()
    initialize_database(DbSettings(url=dsn))
    return dsn


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["extraction", "checkpoint"])
async def test_each_message_is_acknowledged_and_retried_independently(
    tmp_path, monkeypatch, failure
):
    """Later successes never consume an earlier failed target, including empty results."""
    dsn = _seed_unified_database(tmp_path / "unified.sqlite3")
    with get_db(dsn) as connection:
        connection.execute(
            """INSERT INTO chats
            (id, session_id, tenant_id, workspace_id, user_id, sequence, user_input,
             status, completed_at, updated_at, created_at)
            VALUES ('chat-3', 'session-1', 'tenant-1', 'workspace-1', 'user-1', 2,
                    'Keep the next preference.', 'completed', ?, ?, ?)""",
            ("2026-08-31T14:01:00Z",) * 3,
        )
        connection.commit()
    runtime = create_context_runtime(
        _settings(), driver_factory=CapturingDriverFactory()
    )
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    monkeypatch.setattr(
        runtime,
        "models",
        lambda context: StaticModelService(FakeLlmCaller(), user_id=context.user_id),
    )
    calls = []
    failing = True

    async def ingest(
        _service,
        messages,
        _read,
        _write,
        *,
        occurred_at,
        target_message_id,
        evidence_times,
        tool_observations,
    ):
        calls.append(target_message_id)
        assert messages[-1]["id"] == target_message_id
        if target_message_id == "host-chat:chat-3:user":
            assert [message["id"] for message in messages] == [
                "host-chat:chat-1:user",
                "host-chat:chat-3:user",
            ]
        if (
            failing
            and failure == "extraction"
            and target_message_id == "host-chat:chat-1:user"
        ):
            raise RuntimeError("extraction failed")

    acknowledge = loader.acknowledge

    def acknowledge_message(scope, message, *, processed_at):
        if (
            failing
            and failure == "checkpoint"
            and message.message_id == "host-chat:chat-1:user"
        ):
            raise RuntimeError("checkpoint write failed")
        acknowledge(scope, message, processed_at=processed_at)

    monkeypatch.setattr(MemoryIngestService, "ingest", ingest)
    monkeypatch.setattr(loader, "acknowledge", acknowledge_message)
    scope = UserHistoryScope("tenant-1", "workspace-1", "user-1")
    cutoff = datetime(2026, 8, 31, 15, tzinfo=UTC)
    try:
        first = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=scope,
        )
        assert first["status"] == "partial_failed"
        assert first["messages_processed"] == first["messages_failed"] == 1
        assert calls == ["host-chat:chat-1:user", "host-chat:chat-3:user"]
        assert [
            message.message_id
            for message in loader.load_pending(cutoff_at=cutoff, target_scope=scope)
            .batches[0]
            .messages
        ] == ["host-chat:chat-1:user"]
        failing = False
        calls.clear()
        retry = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=scope,
        )
        assert retry["status"] == "success"
        assert retry["messages_processed"] == 1
        assert calls == ["host-chat:chat-1:user"]
        assert loader.load_pending(cutoff_at=cutoff, target_scope=scope).batches == ()
    finally:
        await runtime.aclose()
