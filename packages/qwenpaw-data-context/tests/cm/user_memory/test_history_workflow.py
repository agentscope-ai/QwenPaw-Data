"""Integrate chat history, User extraction, graph writes, and checkpoints."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from context_manager.db import initialize_database
from context_manager.process import build_embedding_text
from context_manager.runtime import ContextRuntime, create_context_runtime
from context_manager.schema import (
    HAS_MEMORY,
    USER,
    USER_MEMORY,
)
from context_manager.settings.db import DbSettings
from context_manager.store import NodeDraft
from context_manager.user.ingest import _memory_id
from context_manager.user.loader import (
    ChatLoader,
    ChatLoaderCheckpointRepository,
    UserHistoryScope,
)
from context_manager.user.loader.loader import USER_GRAPH_UPDATER_VERSION
from qwenpaw_data_auth.database import get_db, init_schema

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.test_startup import (
    CapturingDriverFactory,
    _settings,
)
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import FakeLlmCaller, history_adapter


def _history_database(
    path: Path,
    *,
    include_second_user: bool = False,
    include_hybrid_chat: bool = True,
) -> str:
    """Seed one or two authorized users with distinct completed chats."""
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
        completed_at = datetime(2026, 8, 31, 14, tzinfo=UTC)
        alice_inputs = [
            "我叫 Alice，平时在上海工作，时间都按北京时间来。",
            "以后任何写数据操作都先问我，别直接改。",
            "技术问题先说结论，别铺垫太多。",
        ]
        if include_hybrid_chat:
            alice_inputs.append(
                "我们这边 GMV 不算退款单；我说“退款预警”时，"
                "就是让你检查哪些退款订单影响了 GMV。"
            )
        histories = [
            (
                "user-1",
                "alice",
                tuple(alice_inputs),
            )
        ]
        if include_second_user:
            histories.append(
                (
                    "user-2",
                    "bob",
                    (
                        "我叫 Bob，界面和报告都用英式英文。",
                        "金额默认用英镑显示。",
                        "不确定就直接说不确定，别装作知道。",
                    ),
                )
            )
        for user_position, (user_id, username, inputs) in enumerate(
            histories,
            start=1,
        ):
            connection.execute(
                "INSERT INTO users (id, tenant_id, username) VALUES (?, ?, ?)",
                (user_id, "tenant-1", username),
            )
            connection.execute(
                """
                INSERT INTO role_bindings (
                    id, user_id, role, scope_type, scope_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"binding-{user_id}",
                    user_id,
                    "member",
                    "workspace",
                    "workspace-1",
                ),
            )
            session_id = f"session-{user_id}"
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)",
                (session_id, "tenant-1", "workspace-1", user_id),
            )
            for sequence, user_input in enumerate(inputs, start=1):
                timestamp = (
                    completed_at + timedelta(minutes=user_position * 10 + sequence)
                ).isoformat()
                chat_id = (
                    f"chat-{sequence}" if user_id == "user-1" else f"chat-u2-{sequence}"
                )
                connection.execute(
                    """
                    INSERT INTO chats VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?
                    )
                    """,
                    (
                        chat_id,
                        session_id,
                        "tenant-1",
                        "workspace-1",
                        user_id,
                        sequence,
                        user_input,
                        timestamp,
                        timestamp,
                    ),
                )
        connection.commit()
        connection.execute(
            "ALTER TABLE sessions ADD COLUMN created_at TEXT DEFAULT '2026-08-01T00:00:00Z'"
        )
        connection.execute(
            "ALTER TABLE chats ADD COLUMN created_at TEXT DEFAULT '2026-08-31T14:50:00+00:00'"
        )
        connection.execute("UPDATE chats SET created_at=updated_at")
        from cm.user_memory.loader.test_loader import _history_context_tables

        _history_context_tables(connection)
        connection.commit()
    initialize_database(DbSettings(url=dsn))
    return dsn


def _extraction_completion() -> str:
    """Return deterministic model output grounded in the seeded chats."""
    return json.dumps(
        {
            "profile": {
                "display_name": "Alice",
                "timezone": "Asia/Shanghai",
                "source_refs": ["host-chat:chat-1:user"],
            },
            "memories": [
                {
                    "type": "principle",
                    "description": "The agent must ask before writing data.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "host-chat:chat-2:user",
                    "evidence_refs": ["host-chat:chat-2:user"],
                },
                {
                    "type": "preference",
                    "description": "The user prefers concise technical answers "
                    "with conclusions first.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "host-chat:chat-3:user",
                    "evidence_refs": ["host-chat:chat-3:user"],
                },
            ],
        },
        ensure_ascii=False,
    )


def _target_completions(
    response: str, message_ids: Sequence[str], *, user_id: str | None = None
) -> tuple[str, ...]:
    """Partition a fixture response into independently extracted targets."""
    document = json.loads(response)
    profile = document["profile"]
    responses = []
    existing = False
    for message_id in message_ids:
        memories = [
            memory
            for memory in document["memories"]
            if message_id in memory["evidence_refs"]
        ]
        responses.append(
            json.dumps(
                {
                    "profile": (
                        profile
                        if profile and message_id in profile["source_refs"]
                        else None
                    ),
                    "memories": memories,
                }
            )
        )
        if existing and memories and user_id is not None:
            responses.append(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "memory_id": _memory_id(
                                    "workspace-1",
                                    user_id,
                                    NodeDraft(
                                        USER_MEMORY,
                                        "incoming",
                                        {
                                            "type": memory["type"],
                                            "description": memory["description"],
                                        },
                                    ),
                                ),
                                "action": "create",
                                "existing_memory_id": "",
                            }
                            for memory in memories
                        ]
                    }
                )
            )
        existing = existing or bool(memories)
    return tuple(responses)


def _checkpoint_versions(checkpoints, scope):
    return checkpoints.snapshot(scope, updater_version=USER_GRAPH_UPDATER_VERSION)


def _second_user_completion() -> str:
    """Return distinct graph facts for the second seeded user."""
    return json.dumps(
        {
            "profile": {
                "display_name": "Bob",
                "locale": "en-GB",
                "source_refs": ["host-chat:chat-u2-1:user"],
            },
            "memories": [
                {
                    "type": "preference",
                    "description": "The user prefers monetary amounts in GBP.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "host-chat:chat-u2-2:user",
                    "evidence_refs": ["host-chat:chat-u2-2:user"],
                },
                {
                    "type": "principle",
                    "description": "The agent must state uncertainty explicitly.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "host-chat:chat-u2-3:user",
                    "evidence_refs": ["host-chat:chat-u2-3:user"],
                },
            ],
        },
        ensure_ascii=False,
    )


class _QueuedEmbeddingCaller(FakeLlmCaller):
    """Return a different deterministic embedding batch for each call."""

    def __init__(
        self,
        completions: Sequence[str],
        embedding_batches: Sequence[Sequence[Sequence[float]]],
    ) -> None:
        """Queue extraction/selection responses and per-call vectors."""
        super().__init__(completions)
        self._embedding_batches = [
            tuple(tuple(vector) for vector in batch) for batch in embedding_batches
        ]

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Return the next batch while preserving the shared fake's call log."""
        del model
        self.embed_calls.append((tuple(texts), dimensions))
        if not self._embedding_batches:
            raise AssertionError("unexpected embedding request")
        batch = self._embedding_batches.pop(0)
        if len(batch) != len(texts):
            raise AssertionError("embedding fixture count mismatch")
        return batch


def _runtime_with_memory_graph(
    store: MemoryGraphStore,
    models: StaticModelService,
    monkeypatch: pytest.MonkeyPatch,
) -> ContextRuntime:
    """Compose a real runtime facade around the in-memory graph boundary."""
    runtime = create_context_runtime(
        _settings(),
        driver_factory=CapturingDriverFactory(),
    )
    monkeypatch.setattr(
        runtime,
        "issue_graph_read_scope",
        lambda context, **options: store.access.issue_read_scope(
            context,
            **options,
        ),
    )
    monkeypatch.setattr(
        runtime,
        "issue_graph_write_scope",
        lambda context, **options: store.access.issue_write_scope(
            context,
            **options,
        ),
    )
    monkeypatch.setattr(runtime, "graph_store", lambda _scope: store)
    monkeypatch.setattr(runtime, "models", lambda _context: models)
    return runtime


@pytest.mark.asyncio
async def test_chat_history_is_extracted_into_user_graph_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use hermetic adapters while exercising the complete ingest application path."""
    _history_database(tmp_path / "unified.sqlite3")
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)
    store = MemoryGraphStore()
    caller = _QueuedEmbeddingCaller(
        _target_completions(
            _extraction_completion(),
            [f"host-chat:chat-{n}:user" for n in range(1, 5)],
            user_id="user-1",
        ),
        (((1.0, 0.0),), ((1.0, 0.0), (0.0, 1.0)), ((1.0, 0.0),)),
    )
    caller.ready = True
    runtime = _runtime_with_memory_graph(
        store,
        StaticModelService(caller),
        monkeypatch,
    )
    cutoff = datetime(2026, 8, 31, 15, tzinfo=UTC)
    target = UserHistoryScope("tenant-1", "workspace-1", "user-1")

    try:
        first = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=target,
        )
        replay = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=target,
        )
    finally:
        await runtime.aclose()

    read_scope = store.access.issue_read_scope(authorization())
    users = store.list_nodes(
        USER,
        filters={"user_id": "user-1"},
        limit=2,
        scope=read_scope,
    )
    memories = store.list_nodes(USER_MEMORY, scope=read_scope)
    edges = store.list_edges(HAS_MEMORY, scope=read_scope)

    assert first["status"] == "success"
    assert first["users_discovered"] == 1
    assert first["users_succeeded"] == 1
    assert first["messages_processed"] == 4
    assert len(users) == 1
    user = users[0]
    assert json.loads(str(user.properties["profile"])) == {
        "display_name": "Alice",
        "timezone": "Asia/Shanghai",
        "_evidence_at": {
            field: "2026-08-31T14:11:00.000000+00:00"
            for field in ("display_name", "timezone")
        },
    }
    by_type = {str(memory.properties["type"]): memory for memory in memories}
    assert set(by_type) == {"principle", "preference"}
    assert by_type["principle"].properties["source_refs"] == ("host-chat:chat-2:user",)
    assert by_type["preference"].properties["source_refs"] == ("host-chat:chat-3:user",)
    assert len(edges) == 2
    assert len(caller.complete_calls) == 5
    assert len(caller.embed_calls) == 3
    assert len(_checkpoint_versions(checkpoints, target)) == 4

    assert replay["status"] == "success"
    assert replay["users_discovered"] == 0
    assert replay["messages_processed"] == 0
    assert len(caller.complete_calls) == 5


@pytest.mark.asyncio
async def test_multiple_users_have_isolated_memories_edges_and_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run persists two users without sharing private graph state."""
    _history_database(
        tmp_path / "unified.sqlite3",
        include_second_user=True,
    )
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)
    store = MemoryGraphStore()
    caller = _QueuedEmbeddingCaller(
        _target_completions(
            _extraction_completion(),
            [f"host-chat:chat-{n}:user" for n in range(1, 5)],
            user_id="user-1",
        ),
        (((1.0, 0.0),), ((1.0, 0.0), (0.0, 1.0)), ((1.0, 0.0),)),
    )
    bob_caller = _QueuedEmbeddingCaller(
        _target_completions(
            _second_user_completion(),
            [f"host-chat:chat-u2-{n}:user" for n in range(1, 4)],
            user_id="user-2",
        ),
        (((0.6, 0.8),), ((0.6, 0.8), (0.8, 0.6)), ((0.6, 0.8),)),
    )
    managers = {
        "user-1": StaticModelService(caller, user_id="user-1"),
        "user-2": StaticModelService(bob_caller, user_id="user-2"),
    }
    runtime = _runtime_with_memory_graph(
        store,
        managers["user-1"],
        monkeypatch,
    )
    monkeypatch.setattr(runtime, "models", lambda context: managers[context.user_id])
    cutoff = datetime(2026, 8, 31, 15, tzinfo=UTC)
    alice_target = UserHistoryScope("tenant-1", "workspace-1", "user-1")
    bob_target = UserHistoryScope("tenant-1", "workspace-1", "user-2")

    try:
        first = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
        )
        replay = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
        )
    finally:
        await runtime.aclose()

    alice_scope = store.access.issue_read_scope(authorization(user_id="user-1"))
    bob_scope = store.access.issue_read_scope(authorization(user_id="user-2"))
    alice_users = store.list_nodes(USER, scope=alice_scope)
    bob_users = store.list_nodes(USER, scope=bob_scope)
    alice_memories = store.list_nodes(USER_MEMORY, scope=alice_scope)
    bob_memories = store.list_nodes(USER_MEMORY, scope=bob_scope)
    alice_edges = store.list_edges(HAS_MEMORY, scope=alice_scope)
    bob_edges = store.list_edges(HAS_MEMORY, scope=bob_scope)

    assert first["status"] == "success"
    assert first["users_discovered"] == 2
    assert first["users_succeeded"] == 2
    assert first["messages_processed"] == 7
    assert len(caller.complete_calls) == 5
    assert len(bob_caller.complete_calls) == 4
    assert len(caller.embed_calls) == len(bob_caller.embed_calls) == 3
    for memories, client in ((alice_memories, caller), (bob_memories, bob_caller)):
        vectors_by_text = {
            text: (1.0, 0.0) if client is caller else (0.6, 0.8)
            for texts, _dimensions in client.embed_calls
            for text in texts
            if text.startswith("{")
        }
        for memory in memories:
            text = build_embedding_text(USER_MEMORY, memory.properties)
            assert memory.properties["embedding"] == vectors_by_text[text]
            assert (
                memory.properties["embedding_hash"] == sha256(text.encode()).hexdigest()
            )
        assert set(vectors_by_text) == {
            build_embedding_text(USER_MEMORY, memory.properties) for memory in memories
        }

    assert [node.properties["user_id"] for node in alice_users] == ["user-1"]
    assert [node.properties["user_id"] for node in bob_users] == ["user-2"]
    assert json.loads(str(alice_users[0].properties["profile"])) == {
        "display_name": "Alice",
        "timezone": "Asia/Shanghai",
        "_evidence_at": {
            field: "2026-08-31T14:11:00.000000+00:00"
            for field in ("display_name", "timezone")
        },
    }
    assert json.loads(str(bob_users[0].properties["profile"])) == {
        "display_name": "Bob",
        "locale": "en-GB",
        "_evidence_at": {
            field: "2026-08-31T14:21:00.000000+00:00"
            for field in ("display_name", "locale")
        },
    }
    assert {str(memory.properties["description"]) for memory in alice_memories} == {
        "The agent must ask before writing data.",
        "The user prefers concise technical answers with conclusions first.",
    }
    assert {str(memory.properties["description"]) for memory in bob_memories} == {
        "The user prefers monetary amounts in GBP.",
        "The agent must state uncertainty explicitly.",
    }
    assert {str(memory.properties["id"]) for memory in alice_memories}.isdisjoint(
        str(memory.properties["id"]) for memory in bob_memories
    )
    assert all(memory.properties["owner_id"] == "user-1" for memory in alice_memories)
    assert all(memory.properties["owner_id"] == "user-2" for memory in bob_memories)
    assert all(
        store.get_nodes(USER_MEMORY, (memory.key,), scope=alice_scope)[0] is None
        for memory in bob_memories
    )
    assert all(
        store.get_nodes(USER_MEMORY, (memory.key,), scope=bob_scope)[0] is None
        for memory in alice_memories
    )
    assert len(alice_edges) == 2
    assert len(bob_edges) == 2
    assert {edge.source_key for edge in alice_edges} == {alice_users[0].key}
    assert {edge.source_key for edge in bob_edges} == {bob_users[0].key}
    assert all(
        store.get_edges(
            HAS_MEMORY, ((edge.source_key, edge.target_key),), scope=alice_scope
        )[0]
        is None
        for edge in bob_edges
    )
    assert all(
        store.get_edges(
            HAS_MEMORY, ((edge.source_key, edge.target_key),), scope=bob_scope
        )[0]
        is None
        for edge in alice_edges
    )

    assert len(_checkpoint_versions(checkpoints, alice_target)) == 4
    assert len(_checkpoint_versions(checkpoints, bob_target)) == 3
    assert replay["status"] == "success"
    assert replay["users_discovered"] == 0
    assert len(caller.complete_calls) == 5
    assert len(bob_caller.complete_calls) == 4


@pytest.mark.asyncio
async def test_checkpoint_failure_replays_committed_graph_without_duplicate_facts(
    tmp_path, monkeypatch
):
    """A graph commit followed by a failed acknowledgement is safely replayed."""
    dsn = _history_database(tmp_path / "unified.sqlite3")
    with get_db(dsn) as connection:
        connection.execute("DELETE FROM chats WHERE id <> 'chat-2'")
        connection.commit()
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)
    store = MemoryGraphStore()
    completion = _target_completions(
        _extraction_completion(), ["host-chat:chat-2:user"]
    )[0]
    caller = FakeLlmCaller((completion, completion), ((1.0, 0.0),))
    runtime = _runtime_with_memory_graph(store, StaticModelService(caller), monkeypatch)
    target = UserHistoryScope("tenant-1", "workspace-1", "user-1")
    cutoff = datetime(2026, 8, 31, 15, tzinfo=UTC)
    acknowledge = loader.acknowledge

    def fail_checkpoint(*_args, **_options):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(loader, "acknowledge", fail_checkpoint)
    try:
        before = datetime.now(UTC)
        failed = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=target,
        )
        assert failed["messages_failed"] == 1
        assert failed["messages_processed"] == 0
        assert (
            next(item for item in failed["details"] if item["status"] == "failed")[
                "reason"
            ]
            == "checkpoint_write_failed"
        )
        assert len(store.nodes) == 2 and len(store.edges) == 1
        memory = next(
            values for (spec, _), values in store.nodes.items() if spec is USER_MEMORY
        )
        assert memory["created_at"] >= before
        assert datetime.fromisoformat(memory["evidence_at"]) <= cutoff
        assert _checkpoint_versions(checkpoints, target) == {}
        node_keys, edge_keys = set(store.nodes), set(store.edges)
        monkeypatch.setattr(loader, "acknowledge", acknowledge)
        retry = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=cutoff,
            target_scope=target,
        )
        assert retry["status"] == "success"
        assert retry["messages_processed"] == 1
        assert set(store.nodes) == node_keys
        assert set(store.edges) == edge_keys
        assert len(_checkpoint_versions(checkpoints, target)) == 1
        assert loader.load_pending(cutoff_at=cutoff, target_scope=target).batches == ()
    finally:
        await runtime.aclose()
