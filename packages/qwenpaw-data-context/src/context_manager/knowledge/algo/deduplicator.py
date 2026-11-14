"""Deduplicate Knowledge drafts through KG-owned identity semantics."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime
from typing import cast

from ...model import LlmCaller, TextEmbedder
from ...process import (
    ScoredCandidate,
    SemanticDecisionKind,
    SemanticJudge,
    build_embedding_text,
)
from ...schema import ENTITY, NodeSpec
from ...store import NodeDraft
from ...utils.values import is_empty_value, text_members
from ...utils.vector import cosine_similarity, require_vector_batch
from .judge import KnowledgeSemanticJudge

_LLM_CANDIDATE_LIMIT = 8
_MINIMUM_RECALL_SCORE = 0.4
_DEDUPLICATION_STATUS = "deduplication_status"
_DEDUPLICATION_STATUSES = frozenset({"retained", "ambiguous"})
_NORMALIZE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def identity_parts(spec: NodeSpec, values: Mapping[str, object]) -> tuple[object, ...]:
    """Return the normalized identity tuple shared by exact-key and store key."""
    node_type = cast(str, values["type"])
    if spec is ENTITY:
        return (
            spec.name,
            node_type.casefold(),
            _normalized(cast(str, values["canonical_name"])),
        )
    date_from = values.get("date_from")
    return (
        spec.name,
        node_type.casefold(),
        _normalized(cast(str, values["name"])),
        date_from.isoformat() if isinstance(date_from, date) else "",
        _normalized(str(values.get("scope") or "")),
    )


def identity_hash(spec: NodeSpec, values: Mapping[str, object]) -> str:
    """Return the deterministic store-key hash for one Entity/Event value set."""
    digest = hashlib.sha256(
        json.dumps(identity_parts(spec, values), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def exact_key(draft: NodeDraft) -> tuple[object, ...]:
    """Return a deterministic key for cheap pre-LLM duplicate collapse."""
    return (draft.spec, *identity_parts(draft.spec, draft.properties)[1:])


def judge_document(draft: NodeDraft) -> Mapping[str, object]:
    """Project canonical business fields for identity judgment."""
    return {
        "node": draft.spec.name,
        **dict(cast(Mapping[str, object], _json_value(draft.properties))),
    }


def _can_compare(subject: NodeDraft, candidate: NodeDraft) -> bool:
    """Restrict semantic recall to one canonical spec and exact type."""
    return (
        subject.spec is candidate.spec
        and subject.properties["type"] == candidate.properties["type"]
    )


def _merge_nodes(left: NodeDraft, right: NodeDraft) -> NodeDraft:
    """Merge two validated Drafts already judged to denote one node."""
    if not _can_compare(left, right):
        raise TypeError("cannot merge different Knowledge draft kinds")
    values = dict(left.properties)
    for name, value in right.properties.items():
        if is_empty_value(values.get(name)) and not is_empty_value(value):
            values[name] = value
    if left.spec is ENTITY:
        values["aliases"] = tuple(
            dict.fromkeys(
                (
                    *text_members(left.properties.get("aliases", ())),
                    *text_members(right.properties.get("aliases", ())),
                )
            )
        )
    return NodeDraft(
        left.spec,
        left.ref,
        values,
        transient={
            "confidence": max(
                cast(float, left.transient["confidence"]),
                cast(float, right.transient["confidence"]),
            ),
            "origin_refs": tuple(
                dict.fromkeys(
                    (
                        *cast(tuple[str, ...], left.transient["origin_refs"]),
                        *cast(tuple[str, ...], right.transient["origin_refs"]),
                    )
                )
            ),
        },
    )


def _json_value(value: object) -> object:
    """Convert immutable Draft values to deterministic JSON values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {name: _json_value(item) for name, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    return value


def _normalized(value: str) -> str:
    """Normalize one name only for deterministic exact-key preprocessing."""
    return _NORMALIZE.sub("", value.casefold())


class KnowledgeDeduplicator:
    """Apply the complete Knowledge-specific node-deduplication policy."""

    def __init__(
        self,
        llm: LlmCaller,
        embedder: TextEmbedder,
        *,
        run_sync: Callable[..., Awaitable],
        embedding_dimensions: int | None = None,
    ) -> None:
        """Bind Knowledge identity rules and their model transport."""
        if not isinstance(llm, LlmCaller):
            raise TypeError("llm must implement LlmCaller")
        if not isinstance(embedder, TextEmbedder):
            raise TypeError("embedder must implement TextEmbedder")
        if embedding_dimensions is not None and (
            isinstance(embedding_dimensions, bool)
            or not isinstance(embedding_dimensions, int)
            or embedding_dimensions <= 0
        ):
            raise ValueError("embedding_dimensions must be a positive integer or None")
        self._embedder = embedder
        self._run_sync = run_sync
        self._embedding_dimensions = embedding_dimensions
        self._identity_judge: SemanticJudge[NodeDraft] = KnowledgeSemanticJudge[
            NodeDraft
        ](
            llm,
            mode="batch_deduplication",
            subject_document=judge_document,
            candidate_document=judge_document,
        )

    async def deduplicate(
        self,
        nodes: Sequence[NodeDraft],
    ) -> Sequence[NodeDraft]:
        """Return retained and ambiguous drafts marked by a string status."""
        values = tuple(nodes)
        representatives = await self._run_sync(self._collapse_exact, values)
        if len(representatives) <= 1:
            return tuple(_mark(node, "retained") for node in representatives)

        texts = await self._run_sync(
            lambda: tuple(
                build_embedding_text(node.spec, node.properties)
                for node in representatives
            )
        )
        embeddings = await self._embedder.embed(
            texts, dimensions=self._embedding_dimensions
        )
        vectors = await self._run_sync(
            require_vector_batch,
            embeddings,
            "Knowledge embeddings",
            expected_count=len(representatives),
        )

        retained: list[NodeDraft] = [representatives[0]]
        retained_vectors: list[tuple[float, ...]] = [vectors[0]]
        ambiguous: list[NodeDraft] = []
        for subject, subject_vector in zip(
            representatives[1:], vectors[1:], strict=True
        ):
            candidates = await self._run_sync(
                self._recall,
                subject,
                subject_vector,
                retained,
                retained_vectors,
            )
            if not candidates:
                retained.append(subject)
                retained_vectors.append(subject_vector)
                continue
            decision = await self._identity_judge.judge(subject, candidates)
            candidate_positions = {
                candidate.candidate_id: int(
                    candidate.candidate_id.removeprefix("candidate-")
                )
                for candidate in candidates
            }
            if set(decision.candidate_ids) - set(candidate_positions):
                raise ValueError("semantic judge selected an unknown candidate")
            if decision.kind is SemanticDecisionKind.MATCH:
                position = candidate_positions[decision.candidate_ids[0]]
                retained[position] = await self._run_sync(
                    _merge_nodes, retained[position], subject
                )
            elif decision.kind is SemanticDecisionKind.NEW:
                retained.append(subject)
                retained_vectors.append(subject_vector)
            else:
                ambiguous.append(subject)

        return (
            *(_mark(node, "retained") for node in retained),
            *(_mark(node, "ambiguous") for node in ambiguous),
        )

    def _collapse_exact(
        self,
        nodes: Sequence[NodeDraft],
    ) -> tuple[NodeDraft, ...]:
        """Merge deterministic duplicates before any embedding or model call."""
        positions: dict[tuple[object, ...], int] = {}
        result: list[NodeDraft] = []
        for node in nodes:
            key = exact_key(node)
            position = positions.get(key)
            if position is None:
                positions[key] = len(result)
                result.append(node)
            else:
                result[position] = _merge_nodes(result[position], node)
        return tuple(result)

    def _recall(
        self,
        subject: NodeDraft,
        subject_vector: Sequence[float],
        retained: Sequence[NodeDraft],
        retained_vectors: Sequence[Sequence[float]],
    ) -> tuple[ScoredCandidate[NodeDraft], ...]:
        """Recall a finite same-kind candidate set without deciding identity."""
        ranked = sorted(
            (
                (
                    cosine_similarity(subject_vector, candidate_vector),
                    position,
                    candidate,
                )
                for position, (candidate, candidate_vector) in enumerate(
                    zip(retained, retained_vectors, strict=True)
                )
                if _can_compare(subject, candidate)
            ),
            key=lambda item: item[0],
            reverse=True,
        )[:_LLM_CANDIDATE_LIMIT]
        return tuple(
            ScoredCandidate(f"candidate-{position}", candidate, score)
            for score, position, candidate in ranked
            if score >= _MINIMUM_RECALL_SCORE
        )


def _mark(draft: NodeDraft, status: str) -> NodeDraft:
    """Attach an internal deduplication outcome without revalidating the Draft."""
    return NodeDraft(
        draft.spec,
        draft.ref,
        draft.properties,
        transient={**draft.transient, _DEDUPLICATION_STATUS: status},
    )


def deduplication_status(draft: NodeDraft) -> str:
    """Validate the Deduplicator result at the Plan assembly boundary."""
    status = draft.transient.get(_DEDUPLICATION_STATUS)
    if status not in _DEDUPLICATION_STATUSES:
        raise ValueError("Knowledge draft is missing a deduplication status")
    return str(status)


__all__ = [
    "KnowledgeDeduplicator",
    "deduplication_status",
    "exact_key",
    "identity_hash",
    "identity_parts",
    "judge_document",
]
