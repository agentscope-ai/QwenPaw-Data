"""Verify schema-driven text, immutable enrichment and failure boundaries."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from context_manager.process import EmbeddingEnricher
from context_manager.schema import USER_MEMORY
from context_manager.store import NodeDraft


class RecordingEmbedder:
    """Return deterministic vectors while recording the exact provider inputs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def vector(text: str) -> tuple[float, ...]:
        return (float(len(text)), float(sum(map(ord, text))))

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        assert dimensions is None and model is None
        self.calls.append(tuple(texts))
        return tuple(self.vector(text) for text in texts)

    async def aclose(self) -> None:
        """Release no resources for the in-memory embedder."""


@pytest.mark.asyncio
async def test_empty_text_clears_stale_vectors_without_a_provider_call() -> None:
    embedder = RecordingEmbedder()
    empty = NodeDraft(
        spec=USER_MEMORY,
        ref="memory:empty",
        properties={
            "id": "m1",
            "description": "  ",
            "triggers": ["", "  "],
            "embedding": (9.0, 8.0),
            "embedding_hash": "stale",
        },
    )
    missing = NodeDraft(spec=USER_MEMORY, ref="memory:missing", properties={"id": "m2"})
    enricher = EmbeddingEnricher(embedder)

    assert await enricher.enrich(()) == ()
    result = await enricher.enrich((empty, missing))

    assert embedder.calls == []
    for node in result:
        assert node.properties["embedding"] is None
        assert node.properties["embedding_hash"] is None
    assert empty.properties["embedding_hash"] == "stale"
