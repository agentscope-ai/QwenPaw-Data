"""ConflictArbiter — decision tree for conflict resolution.

Decision tree (see spec §5.1):
  1. Same content_hash → NOOP
  2. Confidence delta ≥ threshold → SUPERSEDE
  3. Same key + different values + both have evidence → CONTRADICTS
  4. Otherwise → PENDING_REVIEW
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from ..utils import get_logger
from .model import ConflictCandidate, ConflictDecision, ConflictEvent, ConflictType

log = get_logger("conflict.arbiter")

DEFAULT_CONFIDENCE_DELTA = 0.2
DEFAULT_HIGH_CONFIDENCE = 0.8
DEFAULT_ANN_SIMILARITY = 0.92


@dataclass
class ArbitrationResult:
    decision: ConflictDecision
    confidence: float
    winner_id: Optional[str]

    def __iter__(self) -> Iterator:
        return iter((self.decision, self.confidence, self.winner_id))


class ConflictArbiter:
    def __init__(
        self,
        confidence_delta_threshold: float = DEFAULT_CONFIDENCE_DELTA,
        high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE,
        ann_similarity_threshold: float = DEFAULT_ANN_SIMILARITY,
    ) -> None:
        self.confidence_delta_threshold = confidence_delta_threshold
        self.high_confidence_threshold = high_confidence_threshold
        self.ann_similarity_threshold = ann_similarity_threshold

    def decide(self, event: ConflictEvent, force_pending: bool = False) -> ArbitrationResult:
        if force_pending:
            log.info("PENDING_REVIEW (forced): high-sensitivity node %s", event.node_id)
            return ArbitrationResult(ConflictDecision.PENDING_REVIEW, 0.0, None)

        # C1 factual conflicts always go to human review
        if event.conflict_type == ConflictType.C1_FACTUAL:
            log.info("PENDING_REVIEW (C1 factual): node %s", event.node_id)
            return ArbitrationResult(ConflictDecision.PENDING_REVIEW, 0.0, None)

        candidates = event.candidates
        if len(candidates) < 2:
            return ArbitrationResult(ConflictDecision.NOOP, 1.0, None)

        # 1. Same content_hash → NOOP
        hashes = {
            c.evidence.get("content_hash")
            for c in candidates
            if c.evidence.get("content_hash")
        }
        if len(hashes) == 1:
            return ArbitrationResult(ConflictDecision.NOOP, 1.0, None)

        # 2. Sort by confidence descending
        sorted_candidates = sorted(candidates, key=lambda c: c.confidence, reverse=True)
        top = sorted_candidates[0]
        second = sorted_candidates[1]
        delta = top.confidence - second.confidence

        # 3. Confidence delta ≥ threshold → SUPERSEDE
        if delta >= self.confidence_delta_threshold:
            log.info(
                "SUPERSEDE: %s (confidence=%.2f) beats %s (delta=%.2f)",
                top.node_id, top.confidence, second.node_id, delta,
            )
            return ArbitrationResult(
                ConflictDecision.SUPERSEDE,
                confidence=top.confidence,
                winner_id=top.node_id,
            )

        # 4. Same key + different values + both have evidence → CONTRADICTS
        values = [c.evidence.get("value") for c in candidates if "value" in c.evidence]
        has_evidence = all(bool(c.evidence) for c in candidates)
        if len(set(values)) > 1 and has_evidence:
            log.info("CONTRADICTS: %d candidates with conflicted values", len(candidates))
            return ArbitrationResult(
                ConflictDecision.CONTRADICTS,
                confidence=max(c.confidence for c in candidates),
                winner_id=None,
            )

        # 5. Otherwise → PENDING_REVIEW
        log.info("PENDING_REVIEW: insufficient evidence for auto-decision")
        return ArbitrationResult(ConflictDecision.PENDING_REVIEW, 0.0, None)
