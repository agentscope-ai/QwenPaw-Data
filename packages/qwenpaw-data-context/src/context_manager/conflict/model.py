"""Conflict arbitration data model.

Shared types for the three-graph (MG/TG/KG) conflict protocol.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


@dataclass
class ConflictCandidate:
    """One candidate node in a conflict."""

    node_id: str
    confidence: float  # Per-graph: KG=trust, TG=success_rate, MG=source_weight
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictEvent:
    """Conflict event emitted by detector, consumed by arbiter."""

    graph: Literal["MG", "TG", "KG"]
    node_id: str
    conflict_type: ConflictType | str
    candidates: list[ConflictCandidate]
    source: Literal["write", "scan"]
    detected_at: dt.datetime = field(default_factory=dt.datetime.now)


class ConflictType(str, Enum):
    """Conflict taxonomy."""

    C1_FACTUAL = "c1_factual"      # Same entity, factual property differs
    C2_DUPLICATE = "c2_duplicate"  # Different keys, same content or canonical_name+type
    C3_ZOMBIE = "c3_zombie"        # Stale/superseded node still active


class ConflictDecision(str, Enum):
    """Arbiter decision outcome."""

    SUPERSEDE = "supersede"          # New wins over old
    CONTRADICTS = "contradicts"      # Mark contradiction, no winner
    NOOP = "noop"                    # No conflict detected, pass through
    PENDING_REVIEW = "pending_review"  # Insufficient evidence, queue for review


@dataclass
class ConflictEntry:
    """Neo4j-backed conflict queue entry."""

    queue_id: str
    graph: Literal["MG", "TG", "KG"]
    event: ConflictEvent
    status: Literal["pending", "decided", "reviewed"] = "pending"
    decision: Optional[ConflictDecision] = None
    winner_id: Optional[str] = None
    machine_confidence: Optional[float] = None
    review_confidence: Optional[float] = None
    reviewer: Optional[str] = None
    review_notes: Optional[str] = None
    created_at: dt.datetime = field(default_factory=dt.datetime.now)
    updated_at: dt.datetime = field(default_factory=dt.datetime.now)

    @staticmethod
    def generate_id() -> str:
        return f"cq-{uuid.uuid4().hex[:12]}"
