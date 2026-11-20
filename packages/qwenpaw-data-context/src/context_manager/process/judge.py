"""Define node-identity judgment contracts and structured decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

from ..utils.validation import require_probability, require_trimmed_text
from ..store.models import NodeDraft
from .search import ScoredCandidate

_CandidateT = TypeVar("_CandidateT")


class SemanticDecisionKind(StrEnum):
    """Describe an identity judgment over a finite candidate set."""

    MATCH = "match"
    NEW = "new"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    """Return one strictly structured node-identity judgment."""

    kind: SemanticDecisionKind
    candidate_ids: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        """Keep decision kind, identifiers, and confidence consistent."""
        if not isinstance(self.kind, SemanticDecisionKind):
            object.__setattr__(self, "kind", SemanticDecisionKind(self.kind))
        candidate_ids = tuple(self.candidate_ids)
        candidate_ids = tuple(
            require_trimmed_text(candidate_id, "decision candidate_id")
            for candidate_id in candidate_ids
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("decision candidate_ids cannot contain duplicates")
        if self.kind is SemanticDecisionKind.MATCH and len(candidate_ids) != 1:
            raise ValueError("match decisions require exactly one candidate_id")
        if self.kind is SemanticDecisionKind.NEW and candidate_ids:
            raise ValueError("new decisions cannot select candidates")
        if self.confidence is not None:
            object.__setattr__(
                self,
                "confidence",
                require_probability(self.confidence, "decision confidence"),
            )
        object.__setattr__(self, "candidate_ids", candidate_ids)


@runtime_checkable
class SemanticJudge(Protocol[_CandidateT]):
    """Judge whether one node draft denotes any recalled candidate."""

    async def judge(
        self,
        subject: NodeDraft,
        candidates: Sequence[ScoredCandidate[_CandidateT]],
    ) -> SemanticDecision:
        """Return match, new, or ambiguous without mutating candidates."""
        ...


__all__ = [
    "SemanticDecision",
    "SemanticDecisionKind",
    "SemanticJudge",
]
