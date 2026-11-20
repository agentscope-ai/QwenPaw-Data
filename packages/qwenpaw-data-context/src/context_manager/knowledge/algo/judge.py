"""Judge Knowledge node identity over finite caller-supplied candidates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...model import LlmCaller, LlmError
from ...process import (
    ScoredCandidate,
    SemanticDecision,
    SemanticDecisionKind,
    complete_json,
)
from ...settings import get_settings
from ...store import NodeDraft
from .contract import KnowledgeImportError, KnowledgeImportErrorCode
from .prompts import knowledge_identity_messages

_CandidateT = TypeVar("_CandidateT")


class _IdentityResponse(BaseModel):
    """Validate the strict private response returned by a Knowledge judge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["match", "new", "ambiguous"]
    candidate_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class KnowledgeSemanticJudge(Generic[_CandidateT]):
    """Ask an LLM to judge identity over a finite Knowledge candidate set."""

    def __init__(
        self,
        caller: LlmCaller,
        *,
        mode: Literal["batch_deduplication", "graph_resolution"],
        subject_document: Callable[[NodeDraft], Mapping[str, object]],
        candidate_document: Callable[[_CandidateT], Mapping[str, object]],
    ) -> None:
        """Bind the Knowledge prompt mode and candidate serialization strategy."""
        if not isinstance(caller, LlmCaller):
            raise TypeError("caller must implement LlmCaller")
        if mode not in {"batch_deduplication", "graph_resolution"}:
            raise ValueError("unsupported Knowledge judge mode")
        if not callable(subject_document):
            raise TypeError("subject_document must be callable")
        if not callable(candidate_document):
            raise TypeError("candidate_document must be callable")
        self._caller = caller
        self._mode = mode
        self._subject_document = subject_document
        self._candidate_document = candidate_document

    async def judge(
        self,
        subject: NodeDraft,
        candidates: Sequence[ScoredCandidate[_CandidateT]],
    ) -> SemanticDecision:
        """Return a structured identity decision without exposing provider text."""
        if type(subject) is not NodeDraft:
            raise TypeError("subject must be a NodeDraft")
        recalled = tuple(candidates)
        if not recalled:
            raise ValueError("semantic judgment requires candidates")
        documents = tuple(
            {
                "candidate_id": candidate.candidate_id,
                "similarity": candidate.similarity,
                "fact": dict(self._candidate_document(candidate.value)),
            }
            for candidate in recalled
        )
        try:
            response = await complete_json(
                self._caller,
                knowledge_identity_messages(
                    self._subject_document(subject),
                    documents,
                    mode=self._mode,
                ),
                attempts=1,
                max_tokens=600,
                timeout_seconds=get_settings().knowledge.ingest_llm_timeout,
            )
            parsed = _IdentityResponse.model_validate(response)
            return SemanticDecision(
                SemanticDecisionKind(parsed.decision),
                tuple(parsed.candidate_ids),
                parsed.confidence,
            )
        except LlmError as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge identity judgment failed",
            ) from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_INVALID,
                "knowledge identity judgment returned an invalid structure",
            ) from exc


__all__ = ["KnowledgeSemanticJudge"]
