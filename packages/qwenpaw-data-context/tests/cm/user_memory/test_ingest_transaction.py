"""Recheck prepared UG evidence against facts changed before transaction entry."""

import json

import pytest
from context_manager.process import build_embedding_text
from context_manager.schema import (
    USER,
    USER_MEMORY,
)
from context_manager.user import MemoryIngestService
from context_manager.user.algo.reconciliation import UserMemoryReconciler
from context_manager.user.algo.resolver import UserMemoryResolver

from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import StaticModelService
from cm.user_memory.test_reconciliation import (
    NEW,
    _decision,
    _EmbeddingCaller,
    _extraction,
    _graph,
)

LATER = "2026-09-01T10:00:00.000000+00:00"


async def _replacement(monkeypatch):
    store, read, write, current, obsolete, targets = _graph()
    caller = _EmbeddingCaller((_extraction(),))
    service = MemoryIngestService(
        store,
        StaticModelService(caller),
        executor=executor,
    )

    async def classify(self, comparisons):
        return [_decision(comparisons[0]["memory_id"], retired=("obsolete",))]

    async def resolve(self, memories):
        return tuple((targets[1],) for _ in memories)

    monkeypatch.setattr(UserMemoryReconciler, "_classify", classify)
    monkeypatch.setattr(UserMemoryResolver, "resolve_batch", resolve)
    plan = await service.prepare_plan(
        ({"id": "target", "text": "Use yuan from now on.", "timestamp": NEW},),
        read,
        target_message_id="target",
    )
    return store, read, write, current, obsolete, caller, service, plan


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("canonical", "obsolete"))
async def test_newer_evidence_after_preparation_skips_the_whole_replacement(
    monkeypatch, changed
):
    store, read, write, current, obsolete, caller, service, plan = await _replacement(
        monkeypatch
    )
    target = current if changed == "canonical" else obsolete
    store.patch_nodes(
        USER_MEMORY,
        (
            (
                target.key,
                {
                    "description": "A later convention has already been adopted.",
                    "evidence_at": LATER,
                },
            ),
        ),
        scope=write,
    )
    facts = {key: dict(value) for key, value in store.nodes.items()}
    edges = {key: dict(value) for key, value in store.edges.items()}
    embedding_calls = len(caller.embed_calls)
    await service.execute_plan(plan, write)
    assert len(caller.embed_calls) == embedding_calls + 1
    assert set(caller.embed_calls[-1][0]) == {
        build_embedding_text(USER_MEMORY, values)
        for (spec, _), values in facts.items()
        if spec is USER_MEMORY
    }
    for key, values in facts.items():
        if key[0] is USER:
            continue  # Profile initialization is independent of stale memories.
        assert {
            name: value
            for name, value in store.nodes[key].items()
            if not name.startswith("embedding")
        } == values
    assert store.edges == edges
    assert len(caller.complete_calls) == 1  # Transaction rechecks call no LLM.
    assert (
        store.get_nodes(USER_MEMORY, (obsolete.key,), scope=read)[0].properties[
            "status"
        ]
        == "active"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("canonical", "obsolete"))
async def test_changed_meaning_without_newer_evidence_conflicts_atomically(
    monkeypatch, changed
):
    store, _read, write, current, obsolete, caller, service, plan = await _replacement(
        monkeypatch
    )
    target = current if changed == "canonical" else obsolete
    store.patch_nodes(
        USER_MEMORY,
        ((target.key, {"description": "Another meaning after classification."}),),
        scope=write,
    )
    facts = {key: dict(value) for key, value in store.nodes.items()}
    edges = {key: dict(value) for key, value in store.edges.items()}
    with pytest.raises(RuntimeError, match="changed.*retry source"):
        await service.execute_plan(plan, write)
    assert store.nodes == facts and store.edges == edges
    assert len(caller.complete_calls) == 1


@pytest.mark.asyncio
async def test_merge_preserves_evidence_appended_after_preparation(monkeypatch):
    store, read, write, current, _obsolete, _targets = _graph()
    caller = _EmbeddingCaller((_extraction(),))
    service = MemoryIngestService(
        store,
        StaticModelService(caller),
        executor=executor,
    )

    async def classify(self, comparisons):
        return [_decision(comparisons[0]["memory_id"], action="merge")]

    monkeypatch.setattr(UserMemoryReconciler, "_classify", classify)
    plan = await service.prepare_plan(
        ({"id": "target", "text": "Use yuan from now on.", "timestamp": NEW},),
        read,
        target_message_id="target",
    )
    store.patch_nodes(
        USER_MEMORY,
        ((current.key, {"source_refs": ("source:existing", "concurrent")}),),
        scope=write,
    )
    await service.execute_plan(plan, write)
    row = store.get_nodes(USER_MEMORY, (current.key,), scope=read)[0]
    assert row.properties["source_refs"] == ("source:existing", "concurrent", "target")
    assert row.properties["description"] == current.properties["description"]


@pytest.mark.asyncio
async def test_profile_rechecks_clocks_per_field_inside_the_transaction():
    store, read, write, *_ = _graph()
    response = json.dumps(
        {
            "profile": {
                "display_name": "Incoming name",
                "locale": "en-GB",
                "source_refs": ["target"],
            },
            "memories": [],
        }
    )
    caller = _EmbeddingCaller((response,))
    service = MemoryIngestService(
        store,
        StaticModelService(caller),
        executor=executor,
    )
    plan = await service.prepare_plan(
        ({"id": "target", "text": "My profile changed.", "timestamp": NEW},),
        read,
        target_message_id="target",
    )
    user = store.list_nodes(USER, scope=read)[0]
    store.patch_nodes(
        USER,
        (
            (
                user.key,
                {
                    "profile": json.dumps(
                        {
                            "display_name": "Latest name",
                            "timezone": None,
                            "_evidence_at": {"display_name": LATER, "timezone": LATER},
                        }
                    )
                },
            ),
        ),
        scope=write,
    )
    await service.execute_plan(plan, write)
    profile = json.loads(
        store.get_nodes(USER, (user.key,), scope=read)[0].properties["profile"]
    )
    assert profile["display_name"] == "Latest name" and profile["timezone"] is None
    assert profile["locale"] == "en-GB"
    assert profile["_evidence_at"]["locale"] == NEW
