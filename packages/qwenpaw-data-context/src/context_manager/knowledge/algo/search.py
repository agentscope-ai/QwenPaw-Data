"""Recall scoped Knowledge candidates and cache request-local embeddings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime
from hashlib import sha256

from ...model import TextEmbedder, LlmError
from ...process import ScoredCandidate, build_embedding_text
from ...schema import NodeSpec
from ...auth import ScopeBinding
from ...store import GraphStore, NodeDraft, NodeRecord
from ...utils.validation import require_integer, require_trimmed_text
from ...utils.vector import (
    cosine_similarity,
    require_finite_vector,
    require_vector_batch,
)
from .contract import KnowledgeImportError, KnowledgeImportErrorCode


class KnowledgeCandidateSearch:
    """Recall scoped graph nodes and cache request-local candidate embeddings."""

    def __init__(
        self,
        store: GraphStore,
        scope: ScopeBinding,
        caller: TextEmbedder,
        *,
        run_sync: Callable[..., Awaitable],
        candidate_limit: int,
        dimensions: int | None = None,
    ) -> None:
        """Bind one request's Store scope, finite pool, and embedding transport."""
        if not isinstance(store, GraphStore):
            raise TypeError("store must implement GraphStore")
        if type(scope) is not ScopeBinding:
            raise TypeError("scope must be a ScopeBinding")
        if not isinstance(caller, TextEmbedder):
            raise TypeError("caller must implement TextEmbedder")
        candidate_limit = require_integer(
            candidate_limit,
            "candidate_limit",
            minimum=1,
            maximum=999,
        )
        if dimensions is not None and dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._store = store
        self._run_sync = run_sync
        self._scope = scope
        self._caller = caller
        self._candidate_limit = candidate_limit
        self._dimensions = dimensions
        self._records: dict[tuple[NodeSpec, str], tuple[NodeRecord, ...]] = {}
        self._vectors: dict[tuple[NodeSpec, str], tuple[float, ...]] = {}

    async def search(
        self,
        subject: NodeDraft,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> Sequence[ScoredCandidate[NodeRecord]]:
        """Rank same-type scoped nodes without deciding their identity."""
        if type(subject) is not NodeDraft:
            raise TypeError("subject must be a NodeDraft")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        type_value = require_trimmed_text(
            subject.properties.get("type"),
            "subject type",
        )
        subject_vector = require_finite_vector(embedding, "embedding")
        key = (subject.spec, type_value)
        records = self._records.get(key)
        if records is None:
            values = tuple(
                await self._run_sync(
                    self._store.list_nodes,
                    subject.spec,
                    filters={"type": type_value},
                    limit=self._candidate_limit + 1,
                    scope=self._scope,
                )
            )
            if len(values) > self._candidate_limit:
                raise KnowledgeImportError(
                    KnowledgeImportErrorCode.CANDIDATE_LIMIT,
                    "existing knowledge candidates exceed the resolver limit",
                )
            if any(
                record.spec is not subject.spec
                or record.properties.get("type") != type_value
                for record in values
            ):
                raise KnowledgeImportError(
                    KnowledgeImportErrorCode.WRITE_FAILED,
                    "knowledge candidate read violated its requested scope",
                )
            records = values
            self._records[key] = records
        await self._ensure_vectors(records, dimensions=len(subject_vector))
        return await self._run_sync(self._rank, records, subject_vector, limit)

    def _rank(self, records, subject_vector, limit):
        try:
            ranked = sorted(
                (
                    (
                        cosine_similarity(
                            subject_vector,
                            self._vectors[_record_key(record)],
                        ),
                        record,
                    )
                    for record in records
                ),
                key=lambda item: item[0],
                reverse=True,
            )[:limit]
        except ValueError as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge embeddings have inconsistent dimensions",
            ) from exc
        return tuple(
            ScoredCandidate(f"candidate-{index}", record, score)
            for index, (score, record) in enumerate(ranked)
        )

    async def _ensure_vectors(
        self,
        records: Sequence[NodeRecord],
        *,
        dimensions: int,
    ) -> None:
        """Reuse schema-text-matched vectors and cache request-local replacements."""
        missing = await self._run_sync(self._missing_vectors, records, dimensions)
        if not missing:
            return
        try:
            generated = await self._caller.embed(
                tuple(text for _, text in missing),
                dimensions=self._dimensions,
            )
        except LlmError as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge candidate embedding request failed",
            ) from exc
        await self._run_sync(self._cache_vectors, missing, generated, dimensions)

    def _missing_vectors(self, records, dimensions):
        missing: list[tuple[NodeRecord, str]] = []
        for record in records:
            key = _record_key(record)
            if key in self._vectors:
                continue
            text = build_embedding_text(record.spec, record.properties)
            existing = _record_vector(record)
            if (
                existing is not None
                and len(existing) == dimensions
                and record.properties.get("embedding_hash")
                == sha256(text.encode("utf-8")).hexdigest()
            ):
                self._vectors[key] = existing
            else:
                missing.append((record, text))
        return missing

    def _cache_vectors(self, missing, generated, dimensions):
        try:
            vectors = require_vector_batch(
                generated,
                "knowledge embeddings",
                expected_count=len(missing),
            )
        except (TypeError, ValueError) as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge embeddings are invalid",
            ) from exc
        if any(len(vector) != dimensions for vector in vectors):
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge embeddings have inconsistent dimensions",
            )
        self._vectors.update(
            (_record_key(record), vector)
            for (record, _), vector in zip(missing, vectors, strict=True)
        )


def node_record_document(record: NodeRecord) -> Mapping[str, object]:
    """Project bounded graph facts for semantic search and identity judgment."""
    values = record.properties
    names = (
        "key",
        "name",
        "canonical_name",
        "type",
        "aliases",
        "description",
        "lifecycle_state",
        "scope",
        "date_from",
        "date_to",
    )
    return {
        "node": record.spec.name,
        **{name: _prompt_value(values[name]) for name in names if name in values},
    }


def _prompt_value(value: object) -> object:
    """Convert bounded Store values to deterministic JSON prompt values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(name): _prompt_value(item) for name, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_prompt_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _record_key(
    record: NodeRecord,
) -> tuple[NodeSpec, str]:
    """Build one cache key from the Store-owned canonical node key."""
    return record.spec, record.key


def _record_vector(record: NodeRecord) -> tuple[float, ...] | None:
    """Return a finite existing embedding or absence when it is unusable."""
    value = record.properties.get("embedding")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        return None
    try:
        return require_finite_vector(value, "embedding")
    except (TypeError, ValueError):
        return None


__all__ = ["KnowledgeCandidateSearch", "node_record_document"]
