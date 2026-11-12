"""Verify the stateful generic process orchestrators."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from context_manager.process import (
    ResolutionKind,
    Resolver,
    ScoredCandidate,
    SemanticDecision,
    SemanticDecisionKind,
)
from context_manager.schema import ENTITY, GRAPH_SCHEMA
from context_manager.store import NodeDraft, NodeRecord


class NamedEmbedder:
    """Map names to deterministic vectors without an external provider."""

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Return close vectors for aliases and an orthogonal vector otherwise."""
        del dimensions, model
        return tuple(
            (1.0, 0.0) if text != "unrelated" else (0.0, 1.0) for text in texts
        )

    async def aclose(self) -> None:
        """Release no resources for the in-memory embedder."""


class RecordingJudge:
    """Return a configured semantic decision and retain finite candidates."""

    def __init__(self, decision: SemanticDecision) -> None:
        """Bind one deterministic model decision."""
        self.decision = decision
        self.calls: list[
            tuple[
                NodeDraft,
                tuple[ScoredCandidate[NodeDraft | NodeRecord], ...],
            ]
        ] = []

    async def judge(
        self,
        subject: NodeDraft,
        candidates: Sequence[ScoredCandidate[NodeDraft | NodeRecord]],
    ) -> SemanticDecision:
        """Record exactly what embedding recall sent to semantic judgment."""
        self.calls.append((subject, tuple(candidates)))
        return self.decision


class RecordingSearch:
    """Return a fixed candidate page for resolver tests."""

    def __init__(self) -> None:
        """Initialize a request log."""
        self.calls: list[tuple[NodeDraft, tuple[float, ...], int]] = []
        self.canonical = NodeRecord(
            ENTITY,
            {
                "key": "entity-existing",
                "name": "canonical",
                "canonical_name": "canonical",
                "type": "product",
            },
        )

    async def search(
        self,
        subject: NodeDraft,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> Sequence[ScoredCandidate[NodeRecord]]:
        """Return one strong and one below-threshold recalled candidate."""
        self.calls.append((subject, tuple(embedding), limit))
        return (
            ScoredCandidate("candidate-a", self.canonical, 0.95),
            ScoredCandidate(
                "candidate-b",
                NodeRecord(
                    ENTITY,
                    {
                        "key": "entity-weak",
                        "name": "weak",
                        "canonical_name": "weak",
                        "type": "product",
                    },
                ),
                0.35,
            ),
        )


def _draft(name: str, sources: tuple[str, ...] = ()) -> NodeDraft:
    """Build one unresolved Entity draft for process tests."""
    return NodeDraft.build(
        GRAPH_SCHEMA,
        ENTITY,
        f"entity:{name}",
        {
            "name": name,
            "canonical_name": name,
            "type": "product",
        },
        transient={"sources": sources},
    )


@pytest.mark.asyncio
async def test_resolver_uses_embedding_only_for_recall_then_llm_filtering() -> None:
    """A high recall score becomes a match only after semantic judgment."""
    search = RecordingSearch()
    judge = RecordingJudge(
        SemanticDecision(SemanticDecisionKind.MATCH, ("candidate-a",), 0.93)
    )
    resolver = Resolver(
        embedder=NamedEmbedder(),
        build_semantic_text=lambda item: str(item.properties["name"]),
        candidate_search=search,
        identity_judge=judge,
    )

    subject = _draft("subject")
    result = await resolver.resolve(subject)

    assert result.kind is ResolutionKind.MATCH
    assert result.match == search.canonical
    assert result.confidence == 0.93
    assert search.calls == [(subject, (1.0, 0.0), 20)]
    assert [item.candidate_id for item in judge.calls[0][1]] == ["candidate-a"]
