"""Verify the sole User Graph algorithm assembly and persistence entrypoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from context_manager.auth import WriteOwner
from context_manager.schema import (
    HAS_MEMORY,
    USER,
    USER_MEMORY,
)
from context_manager.user import MemoryIngestService

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import FakeLlmCaller


def _model_clients(caller: FakeLlmCaller) -> StaticModelService:
    """Expose deterministic clients through the production manager boundary."""
    return StaticModelService(caller)


def _completion() -> str:
    """Return one profile and one non-reference memory extraction."""
    return json.dumps(
        {
            "profile": {
                "display_name": "Alice",
                "source_refs": ["host-chat:chat-1"],
            },
            "memories": [
                {
                    "type": "preference",
                    "description": "The user prefers concise technical answers.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "host-chat:chat-1",
                    "evidence_refs": ["host-chat:chat-1"],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_ingest_atomically_upserts_user_memory_and_replay_edges(
    user_graph,
) -> None:
    """A stable business id prevents duplicate memories and edges on replay."""
    store, _context, read_scope, write_scope = user_graph
    caller = FakeLlmCaller(
        completions=(_completion(), _completion()), embeddings=((1.0, 0.0),)
    )
    service = MemoryIngestService(
        store,
        _model_clients(caller),
        executor=executor,
    )
    messages = (
        {
            "id": "host-chat:chat-1",
            "text": "Please remember that I prefer concise technical answers.",
        },
    )
    occurred_at = datetime(2026, 8, 31, 15, tzinfo=UTC)

    plan = await service.prepare_plan(
        messages,
        read_scope,
        occurred_at=occurred_at,
    )

    assert store.nodes == {}
    await service.execute_plan(plan, write_scope)
    await service.ingest(
        messages,
        read_scope,
        write_scope,
        occurred_at=occurred_at,
    )

    users = store.list_nodes(USER, scope=read_scope)
    memories = store.list_nodes(USER_MEMORY, scope=read_scope)
    edges = store.list_edges(HAS_MEMORY, scope=read_scope)
    assert len(users) == 1
    assert len(memories) == 1
    assert users[0].key == "usr:workspace-1:user-1"
    assert (
        memories[0].key
        == f"mem:{write_scope.write_owner_id}:{memories[0].properties['id']}"
    )
    assert [(edge.source_key, edge.target_key) for edge in edges] == [
        (users[0].key, memories[0].key)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_user", (False, True), ids=("new-user", "existing-user")
)
async def test_profile_merge_preserves_fields_and_receives_complete_snapshot(
    user_graph,
    existing_user: bool,
) -> None:
    """One extraction sees the complete snapshot and retains its latest meaning."""
    store, _context, read_scope, write_scope = user_graph
    occurred_at = datetime(2026, 8, 31, 15, tzinfo=UTC)
    existing_profile = (
        {"locale": "zh-CN", "description": "Data analyst."} if existing_user else {}
    )
    if existing_user:
        store.create_nodes(
            USER,
            (
                {
                    "user_id": "user-1",
                    "profile": json.dumps(existing_profile),
                    "status": "active",
                    "created_at": occurred_at,
                    "updated_at": occurred_at,
                },
            ),
            scope=write_scope,
        )[0]
    caller = FakeLlmCaller(
        completions=(
            json.dumps(
                {
                    "profile": {
                        "display_name": "Alice Chen",
                        "timezone": "UTC",
                        "source_refs": ["host-chat:0", "host-chat:50"],
                    },
                    "memories": [],
                }
            ),
        )
    )
    service = MemoryIngestService(
        store,
        _model_clients(caller),
        executor=executor,
    )
    texts = (
        "我叫 Alice。",
        *("谢谢，明白了。" for _ in range(49)),
        "以后叫我 Alice Chen，我现在在雷克雅未克办公。",
    )
    messages = tuple(
        {"id": f"host-chat:{index}", "text": text} for index, text in enumerate(texts)
    )

    plan = await service.prepare_plan(
        messages,
        read_scope,
        occurred_at=occurred_at,
    )
    await service.execute_plan(plan, write_scope)

    users = store.list_nodes(
        USER,
        filters={"user_id": "user-1"},
        scope=read_scope,
    )
    assert len(users) == 1
    user = users[0]
    assert json.loads(str(user.properties["profile"])) == {
        **existing_profile,
        "display_name": "Alice Chen",
        "timezone": "UTC",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_source", "incoming_source"),
    (("user_implicit", "user_explicit"), ("user_explicit", "user_implicit")),
    ids=("promote-explicit-evidence", "preserve-explicit-evidence"),
)
async def test_same_identity_evidence_and_status_are_merged_at_execution(
    user_graph,
    previous_source: str,
    incoming_source: str,
) -> None:
    """The transaction merges prepared evidence while preserving the stored key."""
    store, _context, read_scope, write_scope = user_graph
    completions = []
    for source_type, refs in (
        (previous_source, ["host-chat:chat-1", "host-chat:chat-2"]),
        (incoming_source, ["host-chat:chat-3", "host-chat:chat-4"]),
    ):
        payload = json.loads(_completion())
        payload["profile"] = None
        payload["memories"][0].update(
            source_type=source_type,
            status="active" if source_type == "user_explicit" else "needs_review",
            source_ref=refs[0],
            evidence_refs=refs,
        )
        completions.append(json.dumps(payload))
    caller = FakeLlmCaller(completions=completions, embeddings=((1.0, 0.0),))
    service = MemoryIngestService(
        store,
        _model_clients(caller),
        executor=executor,
    )
    first_at = datetime(2026, 8, 31, 15, tzinfo=UTC)
    await service.ingest(
        (
            {"id": "host-chat:chat-1", "text": "先说结论就好。"},
            {"id": "host-chat:chat-2", "text": "这次也不用展开那么多。"},
        ),
        read_scope,
        write_scope,
        occurred_at=first_at,
    )
    original = store.list_nodes(USER_MEMORY, scope=read_scope)[0]
    plan = await service.prepare_plan(
        (
            {"id": "host-chat:chat-3", "text": "以后回答技术问题尽量简短。"},
            {"id": "host-chat:chat-4", "text": "对，先给一个简短结论。"},
        ),
        read_scope,
        occurred_at=first_at + timedelta(hours=1),
    )
    assert (
        store.get_nodes(USER_MEMORY, (original.key,), scope=read_scope)[0] == original
    )

    await service.execute_plan(plan, write_scope)

    persisted = store.get_nodes(USER_MEMORY, (original.key,), scope=read_scope)[0]
    assert persisted is not None
    assert persisted.properties["source_refs"] == tuple(
        f"host-chat:chat-{i}" for i in range(1, 5)
    )
    assert persisted.properties["source_type"] == "user_explicit"
    assert persisted.properties["status"] == "active"
    assert persisted.properties["created_at"] == first_at
    assert persisted.properties["updated_at"] == first_at + timedelta(hours=1)
    assert persisted.properties["embedding"] == (1.0, 0.0)
    assert len(store.list_nodes(USER_MEMORY, scope=read_scope)) == 1


@pytest.mark.asyncio
async def test_admin_visibility_does_not_merge_another_owners_matching_business_ids() -> (
    None
):
    """A conflicting identity owned by a peer is neither merged nor duplicated."""
    store = MemoryGraphStore()
    context = authorization(role="ws_admin")
    read_scope = store.access.issue_read_scope(context)
    write_scope = store.access.issue_write_scope(
        context, owner=WriteOwner.USER, zone="user"
    )
    other_scope = store.access.issue_write_scope(
        authorization(user_id="user-2"),
        owner=WriteOwner.USER,
        zone="user",
    )
    service = MemoryIngestService(
        store,
        _model_clients(
            FakeLlmCaller(
                completions=(_completion(), _completion()),
                embeddings=((1.0, 0.0),),
            )
        ),
        executor=executor,
    )
    messages = (
        {"id": "host-chat:chat-1", "text": "我叫 Alice，回答技术问题时简短一点。"},
    )
    preview = await service.prepare_plan(messages, read_scope)
    other_nodes = [
        record
        for spec, properties in (
            (
                USER,
                {"user_id": read_scope.user_id, "profile": "{}", "status": "active"},
            ),
            (USER_MEMORY, preview.memories[0].output()),
        )
        for record in store.get_nodes(
            spec,
            store.create_nodes(spec, (properties,), scope=other_scope),
            scope=other_scope,
        )
    ]

    plan = await service.prepare_plan(messages, read_scope)
    with pytest.raises(RuntimeError, match="duplicate node"):
        await service.execute_plan(plan, write_scope)

    assert len(store.nodes) == 2
    assert store.edges == {}
    for node in other_nodes:
        assert store.get_nodes(node.spec, (node.key,), scope=read_scope)[0] == node
