"""Prepare schema-defined text and enrich immutable node drafts with vectors."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import sha256

from ..model import TextEmbedder
from ..schema import NodeSpec, ValueKind
from ..store.models import NodeDraft
from ..utils.vector import require_vector_batch


def build_embedding_text(spec: NodeSpec, properties: Mapping[str, object]) -> str:
    """Serialize schema-declared fields from draft or persisted node properties."""
    fields: dict[str, str | list[str]] = {}
    for name in spec.embedding_fields:
        value = properties.get(name)
        if value is None:
            continue
        normalized: str | list[str]
        if spec.property(name).kind is ValueKind.TEXT:
            if not isinstance(value, str):
                raise TypeError(f"{spec.name}.{name} must be text")
            normalized = value.strip()
        else:
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes))
                or any(not isinstance(item, str) for item in value)
            ):
                raise TypeError(f"{spec.name}.{name} must be a sequence of text")
            normalized = [item.strip() for item in value if item.strip()]
        if normalized:
            fields[name] = normalized
    return (
        json.dumps(fields, ensure_ascii=False, separators=(",", ":")) if fields else ""
    )


class EmbeddingEnricher:
    """Map schema-defined node text to embedding properties shared by graphs."""

    def __init__(self, embedder: TextEmbedder) -> None:
        """Let the injected embedder own model choices and request batching."""
        if not isinstance(embedder, TextEmbedder):
            raise TypeError("embedder must implement TextEmbedder")
        self._embedder = embedder

    @staticmethod
    def prepare(
        nodes: Sequence[NodeDraft],
    ) -> tuple[tuple[NodeDraft, ...], tuple[tuple[int, str], ...]]:
        """Prepare node copies and indexed model inputs without model I/O."""
        if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
            raise TypeError("nodes must be a sequence of NodeDraft values")
        values = tuple(nodes)
        if any(type(node) is not NodeDraft for node in values):
            raise TypeError("nodes must contain NodeDraft values")
        result = list(values)
        pending: list[tuple[int, str]] = []
        for index, node in enumerate(values):
            if not node.spec.embedding_fields:
                continue
            text = build_embedding_text(node.spec, node.properties)
            if text:
                pending.append((index, text))
            else:
                result[index] = replace(
                    node,
                    properties={
                        **node.properties,
                        "embedding": None,
                        "embedding_hash": None,
                    },
                )
        return tuple(result), tuple(pending)

    @staticmethod
    def apply(
        nodes: tuple[NodeDraft, ...],
        pending: tuple[tuple[int, str], ...],
        embeddings: Sequence[Sequence[float]],
    ) -> tuple[NodeDraft, ...]:
        """Validate model output and build ordered immutable node copies."""
        vectors = require_vector_batch(
            embeddings,
            "embeddings",
            expected_count=len(pending),
        )
        result = list(nodes)
        for (index, text), vector in zip(pending, vectors, strict=True):
            result[index] = replace(
                nodes[index],
                properties={
                    **nodes[index].properties,
                    "embedding": vector,
                    "embedding_hash": sha256(text.encode("utf-8")).hexdigest(),
                },
            )
        return tuple(result)

    async def enrich(
        self,
        nodes: Sequence[NodeDraft],
    ) -> tuple[NodeDraft, ...]:
        """Return ordered enriched copies, or fail the complete enrichment call."""
        prepared, pending = self.prepare(nodes)
        if not pending:
            return prepared
        vectors = await self._embedder.embed(tuple(text for _, text in pending))
        return self.apply(prepared, pending, vectors)


__all__ = ["EmbeddingEnricher", "build_embedding_text"]
