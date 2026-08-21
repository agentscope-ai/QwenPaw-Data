"""ConflictQueue — Neo4j-backed conflict queue with state machine.

State transitions:
    pending -> decided -> reviewed   (machine arbitrates, human reviews)
    pending -> reviewed             (high-sensitivity, skip machine)
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Literal, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .model import (
    ConflictCandidate,
    ConflictDecision,
    ConflictEntry,
    ConflictEvent,
)

log = get_logger("conflict.queue")

_SCHEMA_CYPHER = [
    "CREATE CONSTRAINT conflict_entry_queue_id IF NOT EXISTS "
    "FOR (n:ConflictEntry) REQUIRE n.queue_id IS UNIQUE",
    "CREATE INDEX conflict_entry_status IF NOT EXISTS "
    "FOR (n:ConflictEntry) ON (n.status)",
    "CREATE INDEX conflict_entry_graph IF NOT EXISTS "
    "FOR (n:ConflictEntry) ON (n.graph)",
]

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"decided", "reviewed"},
    "decided": {"reviewed"},
    "reviewed": set(),
}

_ENQUEUE_CYPHER = (
    "MERGE (n:ConflictEntry {queue_id: $queue_id}) "
    "SET n.graph = $graph, "
    "    n.status = $status, "
    "    n.node_id = $node_id, "
    "    n.conflict_type = $conflict_type, "
    "    n.source = $source, "
    "    n.candidates_json = $candidates_json, "
    "    n.detected_at = datetime($detected_at), "
    "    n.created_at = datetime($created_at), "
    "    n.updated_at = datetime($created_at) "
    "RETURN n.queue_id AS queue_id"
)

_UPDATE_STATUS_CYPHER = (
    "MATCH (n:ConflictEntry {queue_id: $queue_id}) "
    "SET n.status = $status, "
    "    n.decision = $decision, "
    "    n.winner_id = $winner_id, "
    "    n.machine_confidence = $machine_confidence, "
    "    n.review_confidence = $review_confidence, "
    "    n.reviewer = $reviewer, "
    "    n.review_notes = $review_notes, "
    "    n.updated_at = datetime($updated_at) "
    "RETURN n"
)

_LIST_ENTRIES_CYPHER = (
    "MATCH (n:ConflictEntry) "
    "WHERE ($status IS NULL OR n.status = $status) "
    "  AND ($graph IS NULL OR n.graph = $graph) "
    "RETURN n.queue_id AS queue_id, n.graph AS graph, n.status AS status, "
    "       n.node_id AS node_id, n.conflict_type AS conflict_type, "
    "       n.candidates_json AS candidates_json, n.decision AS decision, "
    "       n.machine_confidence AS machine_confidence, n.source AS source "
    "ORDER BY n.created_at DESC "
    "LIMIT $limit"
)


class ConflictQueue:
    """Neo4j-backed conflict queue."""

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def init_schema(self) -> None:
        """Create constraints and indexes. Idempotent (IF NOT EXISTS)."""
        with neo4j_session(self.driver) as session:
            for cypher in _SCHEMA_CYPHER:
                session.run(cypher)

    def enqueue(self, event: ConflictEvent) -> str:
        """Insert a conflict event into the queue with status=pending.

        Returns the generated queue_id.
        """
        queue_id = ConflictEntry.generate_id()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        candidates_json = json.dumps(
            [
                {
                    "node_id": c.node_id,
                    "confidence": c.confidence,
                    "evidence": c.evidence,
                }
                for c in event.candidates
            ],
            ensure_ascii=False,
        )

        with neo4j_session(self.driver) as session:
            session.run(
                _ENQUEUE_CYPHER,
                queue_id=queue_id,
                graph=event.graph,
                status="pending",
                node_id=event.node_id,
                conflict_type=event.conflict_type,
                source=event.source,
                candidates_json=candidates_json,
                detected_at=event.detected_at.isoformat(),
                created_at=now,
            )

        log.info(
            "Enqueued conflict %s (graph=%s, type=%s)",
            queue_id,
            event.graph,
            event.conflict_type,
        )
        return queue_id

    def update_status(
        self,
        queue_id: str,
        status: Literal["pending", "decided", "reviewed"],
        decision: Optional[ConflictDecision] = None,
        winner_id: Optional[str] = None,
        machine_confidence: Optional[float] = None,
        review_confidence: Optional[float] = None,
        reviewer: Optional[str] = None,
        review_notes: Optional[str] = None,
    ) -> None:
        """Update queue entry status. Validates state transitions."""
        with neo4j_session(self.driver) as session:
            # Read current status
            result = session.run(
                "MATCH (n:ConflictEntry {queue_id: $qid}) RETURN n.status",
                qid=queue_id,
            )
            row = result.single()
            current = row["n.status"] if row else "pending"

            # Validate transition
            if status not in _VALID_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"Invalid transition: {current} -> {status} for {queue_id}"
                )

            # Apply update
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            session.run(
                _UPDATE_STATUS_CYPHER,
                queue_id=queue_id,
                status=status,
                decision=decision.value if decision else None,
                winner_id=winner_id,
                machine_confidence=machine_confidence,
                review_confidence=review_confidence,
                reviewer=reviewer,
                review_notes=review_notes,
                updated_at=now,
            )

        log.info("Updated %s: %s -> %s", queue_id, current, status)

    def list_entries(
        self,
        status: Optional[str] = None,
        graph: Optional[str] = None,
        limit: int = 50,
    ) -> list[ConflictEntry]:
        """List queue entries, optionally filtered by status and/or graph."""
        with neo4j_session(self.driver) as session:
            result = session.run(
                _LIST_ENTRIES_CYPHER,
                status=status,
                graph=graph,
                limit=limit,
            )
            entries = []
            for row in result:
                candidates = json.loads(row["candidates_json"] or "[]")
                event = ConflictEvent(
                    graph=row["graph"],
                    node_id=row["node_id"],
                    conflict_type=row["conflict_type"],
                    candidates=[ConflictCandidate(**c) for c in candidates],
                    source=row.get("source", "scan"),
                )
                entry_decision = None
                if row.get("decision"):
                    entry_decision = ConflictDecision(row["decision"])
                entries.append(
                    ConflictEntry(
                        queue_id=row["queue_id"],
                        graph=row["graph"],
                        event=event,
                        status=row["status"],
                        decision=entry_decision,
                        machine_confidence=row.get("machine_confidence"),
                    )
                )
            return entries
