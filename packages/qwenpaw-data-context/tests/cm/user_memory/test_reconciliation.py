"""Exercise cross-batch meanings, evidence order and atomic graph replacements."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from context_manager.auth import WriteOwner
from context_manager.schema import (
    ENTITY,
    HAS_MEMORY,
    USER,
    USER_MEMORY,
    USER_MEMORY_REFERS_TO_ENTITY,
)
from context_manager.store import NodeDraft, NodeRecord, StoreError
from context_manager.user import MemoryIngestService
from context_manager.user.algo.reconciliation import UserMemoryReconciler
from context_manager.user.algo.resolver import UserMemoryResolver

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import FakeLlmCaller

OLD = "2026-08-30T10:00:00.000000+00:00"
NEW = "2026-08-31T10:00:00.000000+00:00"


def _draft(
    mid="incoming",
    description="Use yuan for reporting amounts.",
    *,
    stamp=NEW,
    **values,
):
    return NodeDraft(
        USER_MEMORY,
        f"draft:{mid}",
        {
            "id": mid,
            "type": "preference",
            "description": description,
            "source_type": "user_explicit",
            "source_ref": f"source:{mid}",
            "source_refs": (f"source:{mid}",),
            "triggers": (),
            "status": "active",
            **({"evidence_at": stamp} if stamp else {}),
            **values,
        },
    )


def _stored(
    mid="existing", description="Amounts default to ten-thousand yuan.", **values
):
    node = _draft(mid, description, stamp=OLD, **values)
    return NodeRecord(USER_MEMORY, {**node.properties, "key": f"key:{mid}"})


def _decision(mid="incoming", action="replace", target="existing", retired=()):
    return {
        "memory_id": mid,
        "action": action,
        "existing_memory_id": target,
        "superseded_memory_ids": list(retired),
    }


def _response(*decisions):
    return json.dumps({"decisions": decisions})


@pytest.mark.asyncio
async def test_exact_identity_and_shared_trigger_have_priority():
    incoming = _draft("same", type="trigger", triggers=("Ｎet   Revenue",))
    existing = (
        _stored("unrelated"),
        _stored("same"),
        _stored("trigger", triggers=("net revenue",)),
    )
    caller = FakeLlmCaller(
        (_response(_decision("same", "merge", "same")),),
        embeddings=((1.0, 0.0), (-1.0, 0.0)),
    )
    await UserMemoryReconciler(caller, caller, run_sync=asyncio.to_thread).reconcile(
        (incoming,), existing
    )
    candidates = json.loads(caller.complete_calls[0][0][1].content)["memories"][0][
        "candidates"
    ]
    assert [item["id"] for item in candidates] == ["same", "trigger", "unrelated"]
    assert [item["similarity"] for item in candidates] == [1.0, 1.0, -1.0]


@pytest.mark.asyncio
async def test_coalesce_keeps_latest_meaning_and_all_evidence():
    nodes = (
        _draft("first", "Use yuan for amounts.", stamp=OLD, triggers=("first",)),
        _draft("second", "Use dollars for amounts.", triggers=("second",)),
    )
    caller = FakeLlmCaller(
        (_response(_decision("first"), _decision("second")),), embeddings=((1.0,),) * 3
    )
    result = await UserMemoryReconciler(
        caller, caller, run_sync=asyncio.to_thread
    ).reconcile(nodes, (_stored(evidence_at=""),))
    assert (
        len(result) == 1
        and result[0].properties["description"] == nodes[1].properties["description"]
    )
    assert result[0].properties["source_refs"] == ("source:first", "source:second")
    assert result[0].properties["triggers"] == ("first", "second")


class _EmbeddingCaller(FakeLlmCaller):
    async def embed(self, texts, *, dimensions=None, model=None):
        self.embed_calls.append((tuple(texts), dimensions))
        return ((1.0, 0.0),) * len(texts)


def _graph():
    store = MemoryGraphStore()
    context = authorization()
    read = store.access.issue_read_scope(context)
    write = store.access.issue_write_scope(context, owner=WriteOwner.USER, zone="user")
    user = store.get_nodes(
        USER,
        store.create_nodes(
            USER,
            ({"user_id": "user-1", "status": "active", "profile": "{}"},),
            scope=write,
        ),
        scope=write,
    )[0]
    existing, obsolete = store.get_nodes(
        USER_MEMORY,
        store.create_nodes(
            USER_MEMORY,
            tuple(
                {
                    **_draft(mid, "Default to ten-thousand yuan.", stamp=OLD).output(),
                    "updated_at": datetime(2026, 8, 30, tzinfo=UTC),
                }
                for mid in ("existing", "obsolete")
            ),
            scope=write,
        ),
        scope=write,
    )
    store.create_edges(
        HAS_MEMORY,
        ((user.key, existing.key, {}), (user.key, obsolete.key, {})),
        scope=write,
    )
    knowledge = store.access.issue_write_scope(
        context, owner=WriteOwner.USER, zone="knowledge"
    )
    targets = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {
                    "name": "Old governed subject",
                    "type": "term",
                    "identity_hash": "old-subject",
                },
                {
                    "name": "New governed subject",
                    "type": "term",
                    "identity_hash": "new-subject",
                },
            ),
            scope=knowledge,
        ),
        scope=knowledge,
    )
    store.create_edges(
        USER_MEMORY_REFERS_TO_ENTITY, ((existing.key, targets[0].key, {}),), scope=write
    )
    return store, read, write, existing, obsolete, targets


def _extraction():
    return json.dumps(
        {
            "profile": None,
            "memories": [
                {
                    "type": "preference",
                    "description": "Use yuan for reporting amounts.",
                    "source_type": "user_explicit",
                    "status": "active",
                    "source_ref": "target",
                    "evidence_refs": ["target"],
                }
            ],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_replacement_refreshes_references_and_retires_obsolete_meanings_atomically(
    monkeypatch, fail
):
    store, read, write, existing, obsolete, targets = _graph()
    caller = _EmbeddingCaller((_extraction(),))
    service = MemoryIngestService(
        store,
        StaticModelService(caller),
        executor=executor,
    )

    async def classify(self, comparisons):
        return [_decision(comparisons[0]["memory_id"], retired=("obsolete",))]

    async def resolve_batch(self, memories):
        return tuple((targets[1],) for _ in memories)

    monkeypatch.setattr(UserMemoryReconciler, "_classify", classify)
    monkeypatch.setattr(UserMemoryResolver, "resolve_batch", resolve_batch)
    plan = await service.prepare_plan(
        ({"id": "target", "text": "Use yuan from now on.", "timestamp": NEW},),
        read,
        target_message_id="target",
    )
    assert len(plan.memories) == 1 and plan.memories[0].properties["id"] == "existing"
    original = store.create_edges
    before_nodes = {key: dict(value) for key, value in store.nodes.items()}
    before_edges = {key: dict(value) for key, value in store.edges.items()}

    def create_edges(spec, edges, *, fields=None, scope):
        result = original(spec, edges, fields=fields, scope=scope)
        if fail and spec is USER_MEMORY_REFERS_TO_ENTITY:
            raise StoreError("reference write failed", operation="create_edges")
        return result

    monkeypatch.setattr(store, "create_edges", create_edges)
    if fail:
        with pytest.raises(StoreError, match="reference write failed"):
            await service.execute_plan(plan, write)
        assert store.nodes == before_nodes and store.edges == before_edges
        return
    await service.execute_plan(plan, write)
    current, retired = store.get_nodes(
        USER_MEMORY, (existing.key, obsolete.key), scope=read
    )
    assert current.properties["description"] == "Use yuan for reporting amounts."
    assert current.properties["source_refs"] == ("source:existing", "target")
    assert (
        retired.properties["status"] == "revoked"
        and retired.properties["evidence_at"] == NEW
    )
    assert retired.properties["source_refs"] == ("source:obsolete", "target")
    edges = store.list_edges(USER_MEMORY_REFERS_TO_ENTITY, scope=read)
    assert [(edge.source_key, edge.target_key) for edge in edges] == [
        (existing.key, targets[1].key)
    ]
    assert len(store.list_edges(HAS_MEMORY, scope=read)) == 2


@pytest.mark.asyncio
async def test_revoked_memory_is_not_recalled_and_its_retirement_clock_blocks_old_replay(
    monkeypatch,
):
    from context_manager.user.ingest import _memory_id

    store, read, write, _existing, _, _ = _graph()
    retired_id = _memory_id("workspace-1", "user-1", _draft())
    retired = store.get_nodes(
        USER_MEMORY,
        store.create_nodes(
            USER_MEMORY,
            ({**_draft(retired_id).output(), "status": "revoked"},),
            scope=write,
        ),
        scope=write,
    )[0]
    user = store.list_nodes(USER, scope=read)[0]
    store.create_edges(HAS_MEMORY, ((user.key, retired.key, {}),), scope=write)
    caller = _EmbeddingCaller((_extraction(),))

    async def classify(self, comparisons):
        assert all(item["id"] != retired_id for item in comparisons[0]["candidates"])
        return [_decision(comparisons[0]["memory_id"], "create", "")]

    monkeypatch.setattr(UserMemoryReconciler, "_classify", classify)
    service = MemoryIngestService(
        store,
        StaticModelService(caller),
        executor=executor,
    )
    plan = await service.prepare_plan(
        ({"id": "target", "text": "Use yuan.", "timestamp": OLD},),
        read,
        target_message_id="target",
    )
    assert plan.memories[0].properties["id"] == retired_id
    await service.execute_plan(plan, write)
    assert store.get_nodes(USER_MEMORY, (retired.key,), scope=read)[0] == retired
