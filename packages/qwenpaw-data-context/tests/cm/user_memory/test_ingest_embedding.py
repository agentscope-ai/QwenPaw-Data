"""Verify post-ingest embedding repair uses committed facts and shared enrichment."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from context_manager.auth import WriteOwner
from context_manager.process import build_embedding_text
from context_manager.schema import GRAPH_SCHEMA, HAS_MEMORY, USER_MEMORY
from context_manager.store import EdgeDraft, NodeDraft
from context_manager.user import MemoryIngestPlan, MemoryIngestService

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import FakeLlmCaller


def _memory(index=0, **changes):
    return NodeDraft.build(
        GRAPH_SCHEMA,
        USER_MEMORY,
        f"incoming:{index}",
        {
            "id": f"memory-{index}",
            "type": "preference",
            "description": f"Use concise wording in report {index}.",
            "status": "active",
            "source_type": "user_explicit",
            "source_ref": "source-1",
            "source_refs": ("source-1",),
            "triggers": (),
            **changes,
        },
    )


def _plan(caller, *memories):
    return MemoryIngestPlan(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        user_id="user-1",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        profile_delta={},
        profile_source_refs=(),
        memories=memories,
        reference_keys={},
        edges=tuple(
            EdgeDraft(HAS_MEMORY, "user:user-1", memory.ref, {}) for memory in memories
        ),
    )


def _setup(caller):
    store = MemoryGraphStore()
    write = store.access.issue_write_scope(
        authorization(), owner=WriteOwner.USER, zone="user"
    )
    return (
        store,
        write,
        MemoryIngestService(
            store,
            StaticModelService(caller),
            executor=executor,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("provider", "invalid-batch", "write"))
async def test_embedding_failure_keeps_facts_and_later_ingest_repairs_vectors(
    failure, monkeypatch, caplog
):
    caller = FakeLlmCaller(embeddings=((1.0, 0.0),) * 2)
    store, write, service = _setup(caller)
    plan = _plan(caller, _memory(), _memory(1))
    original_embed = caller.embed
    original_patch = store.patch_nodes

    if failure == "provider":

        async def unavailable(*args, **kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(caller, "embed", unavailable)
    elif failure == "invalid-batch":

        async def incomplete(texts, **kwargs):
            return (await original_embed(texts, **kwargs))[:-1]

        monkeypatch.setattr(caller, "embed", incomplete)
    else:

        def unavailable(spec, patches, *, scope):
            if any("embedding" in changes for _, changes in patches):
                raise RuntimeError("embedding write unavailable")
            return original_patch(spec, patches, scope=scope)

        monkeypatch.setattr(store, "patch_nodes", unavailable)

    await service.execute_plan(plan, write)
    assert "post-write embedding failed" in caplog.text
    assert len(store.nodes) == 3 and len(store.edges) == 2
    assert all(
        "embedding" not in node.properties
        for node in store.list_nodes(USER_MEMORY, scope=write)
    )

    monkeypatch.setattr(caller, "embed", original_embed)
    monkeypatch.setattr(store, "patch_nodes", original_patch)
    caller.embeddings = ((1.0, 0.0),) * 2
    before_calls = len(caller.embed_calls)
    await service.execute_plan(plan, write)
    assert len(caller.embed_calls) == before_calls + 1
    assert all(
        node.properties["embedding"] == (1.0, 0.0)
        and node.properties["embedding_hash"]
        == sha256(
            build_embedding_text(USER_MEMORY, node.properties).encode()
        ).hexdigest()
        for node in store.list_nodes(USER_MEMORY, scope=write)
    )
    assert len(store.nodes) == 3 and len(store.edges) == 2
    await service.execute_plan(plan, write)
    assert len(caller.embed_calls) == before_calls + 1
