"""Define scoped existing-node recall for draft resolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..store import NodeDraft, NodeRecord
from ..utils.validation import require_probability, require_trimmed_text

_CandidateT = TypeVar("_CandidateT")


@dataclass(frozen=True, slots=True)
class ScoredCandidate(Generic[_CandidateT]):
    """Carry one candidate and its similarity to one specific subject."""

    candidate_id: str
    value: _CandidateT
    similarity: float

    def __post_init__(self) -> None:
        """Reject identifiers and scores that cannot safely enter a judge."""
        object.__setattr__(
            self,
            "candidate_id",
            require_trimmed_text(self.candidate_id, "candidate_id"),
        )
        object.__setattr__(
            self,
            "similarity",
            require_probability(self.similarity, "candidate similarity"),
        )


@runtime_checkable
class CandidateSearch(Protocol):
    """Recall existing graph nodes that may denote one node draft."""

    async def search(
        self,
        subject: NodeDraft,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> Sequence[ScoredCandidate[NodeRecord]]:
        """Return finite scoped candidates without deciding node identity."""
        ...


__all__ = ["CandidateSearch", "ScoredCandidate"]
