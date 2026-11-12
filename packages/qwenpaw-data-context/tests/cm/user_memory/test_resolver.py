"""Verify bounded UserMemory reference resolution against MG/KG nodes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence

import pytest
from context_manager.auth import GraphScopeAuthority, ScopeBinding
from context_manager.schema import (
    GRAPH_SCHEMA,
    METRIC,
    USER_MEMORY,
    NodeSpec,
)
from context_manager.store import NodeDraft, NodeRecord
from context_manager.user import (
    UserMemoryResolver,
)

from cm.infrastructure.access.support import authorization
from cm.user_memory.support import FakeLlmCaller


def _memory(
    description: str,
    *,
    memory_type: str = "anchored_rule",
    status: str = "active",
    triggers: tuple[str, ...] = (),
    ref: str = "memory-1",
) -> NodeDraft:
    """Build one linkable or deliberately skipped memory."""
    return NodeDraft.build(
        GRAPH_SCHEMA,
        USER_MEMORY,
        ref,
        {
            "type": memory_type,
            "description": description,
            "source_type": "user_explicit",
            "source_refs": ("msg-1",),
            "triggers": triggers,
            "status": status,
        },
    )


def _record(spec: NodeSpec, key: str, name: str) -> NodeRecord:
    """Build one detached candidate with searchable fields."""
    return NodeRecord(
        spec,
        {
            "key": key,
            "name": name,
            "aliases": (),
            "description": f"Canonical {name} object.",
            "embedding": (0.25,) * 1024,
        },
    )


def _scope() -> ScopeBinding:
    """Issue one zone-neutral read scope for cross-graph recall."""
    return GraphScopeAuthority().issue_read_scope(authorization())


class SearchStore:
    """Return configured rank-ordered Store search results."""

    def __init__(self) -> None:
        """Initialize result maps and call observations."""
        self.fulltext: dict[NodeSpec, tuple[NodeRecord, ...]] = {}
        self.vector: dict[NodeSpec, tuple[NodeRecord, ...]] = {}
        self.fulltext_calls: list[tuple[NodeSpec, str, int, ScopeBinding]] = []
        self.vector_calls: list[
            tuple[NodeSpec, tuple[float, ...], int, ScopeBinding]
        ] = []

    def search_fulltext(
        self,
        text: str,
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Return the configured full-text rank for one type."""
        (spec,) = specs
        assert filters is None
        self.fulltext_calls.append((spec, text, limit, scope))
        return tuple(
            record
            if fields is None
            else NodeRecord(
                spec, {name: record.properties.get(name) for name in ("key", *fields)}
            )
            for record in self.fulltext.get(spec, ())
        )

    def search_vector(
        self,
        embedding: Sequence[float],
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Return the configured vector rank for one type."""
        (spec,) = specs
        assert filters is None
        vector = tuple(embedding)
        self.vector_calls.append((spec, vector, limit, scope))
        return tuple(
            record
            if fields is None
            else NodeRecord(
                spec, {name: record.properties.get(name) for name in ("key", *fields)}
            )
            for record in self.vector.get(spec, ())
        )


def _plan(*queries: str) -> str:
    return json.dumps({"plans": [{"memory_id": "memory-1", "queries": list(queries)}]})


def _selection(*keys: str) -> str:
    return json.dumps(
        {"decisions": [{"memory_id": "memory-1", "target_keys": list(keys)}]}
    )


@pytest.mark.asyncio
async def test_selector_ignores_unknown_keys_and_deduplicates_valid_keys() -> None:
    store = SearchStore()
    metric = _record(METRIC, "metric-gmv", "GMV")
    store.fulltext[METRIC] = (metric,)
    caller = FakeLlmCaller(
        (_plan("GMV"), _selection("invented", metric.key, metric.key)),
        ((1.0,),),
    )
    targets = await UserMemoryResolver(
        store, _scope(), caller, caller, run_sync=asyncio.to_thread
    ).resolve(_memory("GMV excludes refunded orders."))
    assert [target.key for target in targets] == [metric.key]


@pytest.mark.asyncio
async def test_candidate_budget_uses_retrieval_ranks_without_exact_name_boost():
    store = SearchStore()
    first = _record(METRIC, "first", "Gross merchandise value")
    exact = _record(METRIC, "exact", "GMV")
    store.fulltext[METRIC] = (first, exact)
    store.vector[METRIC] = (first, exact)
    caller = FakeLlmCaller((_plan("GMV"), _selection(first.key)), ((1.0,),))
    targets = await UserMemoryResolver(
        store,
        _scope(),
        caller,
        caller,
        candidate_limit=1,
        run_sync=asyncio.to_thread,
    ).resolve(_memory("GMV excludes refunded orders."))
    assert [target.key for target in targets] == [first.key]
    request = json.loads(caller.complete_calls[-1][0][-1].content)
    assert [row["key"] for row in request["memories"][0]["candidates"]] == [first.key]
