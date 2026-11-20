"""Resolve one subject through embedding recall and semantic candidate judgment."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ..model import TextEmbedder
from ..store import NodeDraft, NodeRecord
from ..utils.validation import (
    require_integer,
    require_nonempty_text,
    require_probability,
)
from ..utils.vector import require_finite_vector
from .judge import SemanticDecisionKind, SemanticJudge
from .search import CandidateSearch


class ResolutionKind(StrEnum):
    """Describe whether one subject is new, matched, or unsafe to decide."""

    NEW = "new"
    MATCH = "match"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Resolution:
    """Return one validated decision and the subject embedding used for recall."""

    kind: ResolutionKind
    subject_embedding: tuple[float, ...]
    match: NodeRecord | None = None
    candidates: tuple[NodeRecord, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        """Keep match and candidate payloads consistent with the result kind."""
        if not isinstance(self.kind, ResolutionKind):
            object.__setattr__(self, "kind", ResolutionKind(self.kind))
        candidates = tuple(self.candidates)
        if self.match is not None and type(self.match) is not NodeRecord:
            raise TypeError("resolution match must be a NodeRecord")
        if any(type(candidate) is not NodeRecord for candidate in candidates):
            raise TypeError("resolution candidates must contain NodeRecord values")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self,
            "subject_embedding",
            require_finite_vector(self.subject_embedding, "subject_embedding"),
        )
        if self.kind is ResolutionKind.MATCH and self.match is None:
            raise ValueError("matched resolution requires a match")
        if self.kind is not ResolutionKind.MATCH and self.match is not None:
            raise ValueError("only matched resolution may contain a match")


class Resolver:
    """Resolve one node draft against scoped existing node records."""

    def __init__(
        self,
        *,
        embedder: TextEmbedder,
        build_semantic_text: Callable[[NodeDraft], str],
        candidate_search: CandidateSearch,
        identity_judge: SemanticJudge[NodeRecord],
        embedding_dimensions: int | None = None,
    ) -> None:
        """Bind embedding, scoped candidate recall, and semantic judgment."""
        if not isinstance(embedder, TextEmbedder):
            raise TypeError("embedder must implement TextEmbedder")
        if not callable(build_semantic_text):
            raise TypeError("build_semantic_text must be callable")
        if not isinstance(candidate_search, CandidateSearch):
            raise TypeError("candidate_search must implement CandidateSearch")
        if not isinstance(identity_judge, SemanticJudge):
            raise TypeError("identity_judge must implement SemanticJudge")
        if embedding_dimensions is not None and (
            isinstance(embedding_dimensions, bool)
            or not isinstance(embedding_dimensions, int)
            or embedding_dimensions <= 0
        ):
            raise ValueError("embedding_dimensions must be a positive integer or None")
        self._embedder = embedder
        self._build_semantic_text = build_semantic_text
        self._candidate_search = candidate_search
        self._identity_judge = identity_judge
        self._embedding_dimensions = embedding_dimensions

    async def resolve(
        self,
        subject: NodeDraft,
        *,
        search_limit: int = 20,
        llm_candidate_limit: int = 8,
        minimum_recall_score: float = 0.4,
    ) -> Resolution:
        """Recall candidates, then let the semantic judge filter and decide."""
        if type(subject) is not NodeDraft:
            raise TypeError("subject must be a NodeDraft")
        search_limit = require_integer(
            search_limit,
            "search_limit",
            minimum=1,
            maximum=100,
        )
        llm_candidate_limit = require_integer(
            llm_candidate_limit,
            "llm_candidate_limit",
            minimum=1,
            maximum=search_limit,
        )
        minimum_recall_score = require_probability(
            minimum_recall_score,
            "minimum_recall_score",
        )

        text = self._build_semantic_text(subject)
        try:
            require_nonempty_text(text, "semantic text")
        except (TypeError, ValueError) as exc:
            raise ValueError("build_semantic_text must return non-empty text") from exc
        embeddings = await self._embedder.embed(
            (text,),
            dimensions=self._embedding_dimensions,
        )
        if len(embeddings) != 1:
            raise ValueError("embedder must return exactly one subject vector")
        subject_embedding = require_finite_vector(
            embeddings[0],
            "subject embedding",
        )
        recalled = tuple(
            await self._candidate_search.search(
                subject,
                subject_embedding,
                limit=search_limit,
            )
        )
        candidate_ids = [candidate.candidate_id for candidate in recalled]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate search returned duplicate identifiers")
        candidates = tuple(
            sorted(recalled, key=lambda candidate: candidate.similarity, reverse=True)
        )
        candidates = tuple(
            candidate
            for candidate in candidates[:llm_candidate_limit]
            if candidate.similarity >= minimum_recall_score
        )
        if not candidates:
            return Resolution(ResolutionKind.NEW, subject_embedding)

        decision = await self._identity_judge.judge(subject, candidates)
        candidate_by_id = {
            candidate.candidate_id: candidate for candidate in candidates
        }
        unknown = set(decision.candidate_ids) - set(candidate_by_id)
        if unknown:
            raise ValueError("semantic judge selected an unknown candidate")
        if decision.kind is SemanticDecisionKind.NEW:
            return Resolution(
                ResolutionKind.NEW,
                subject_embedding,
                confidence=decision.confidence,
            )
        if decision.kind is SemanticDecisionKind.MATCH:
            match = candidate_by_id[decision.candidate_ids[0]].value
            return Resolution(
                ResolutionKind.MATCH,
                subject_embedding,
                match=match,
                confidence=decision.confidence,
            )
        selected = (
            tuple(candidate_by_id[item].value for item in decision.candidate_ids)
            if decision.candidate_ids
            else tuple(candidate.value for candidate in candidates)
        )
        return Resolution(
            ResolutionKind.AMBIGUOUS,
            subject_embedding,
            candidates=selected,
            confidence=decision.confidence,
        )


__all__ = [
    "Resolution",
    "ResolutionKind",
    "Resolver",
]
