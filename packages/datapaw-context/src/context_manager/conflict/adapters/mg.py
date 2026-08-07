"""MG conflict adapter — semantic node definition conflicts."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from neo4j import Driver

from ...utils import get_logger, neo4j_session
from ..model import ConflictCandidate, ConflictDecision, ConflictEvent

log = get_logger("conflict.adapters.mg")

_CONFLICT_LABELS = {"Metric", "Dimension", "Column", "DatasetColumn"}
_CONFLICT_TYPE_MAP = {
    "Metric": "metric_definition",
    "Dimension": "dimension_type",
    "Column": "column_type",
    "DatasetColumn": "column_mapping",
}

_LOOKUP_CYPHER = (
    "MATCH (n {key: $key}) "
    "WHERE $label IN labels(n) "
    "RETURN n.key AS key, $label AS label, n.content_hash AS content_hash"
)

_MG_SUPERSEDE_CYPHER = (
    "MATCH (old {key: $old_key}), (new {key: $new_key}) "
    "SET old.valid_to = datetime($now) "
    "MERGE (old)-[r:SUPERSEDED_BY]->(new) "
    "SET r.reason = $reason, r.decided_at = datetime($now)"
)

_MG_CONTRADICTS_CYPHER = (
    "MATCH (a {key: $key_a}), (b {key: $key_b}) "
    "MERGE (a)-[r1:CONTRADICTS]->(b) "
    "MERGE (b)-[r2:CONTRADICTS]->(a)"
)


class MGConflictAdapter:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def detect(self, node: dict) -> Optional[ConflictEvent]:
        key = str(node.get("key") or "").strip()
        label = str(node.get("label") or "").strip()
        new_hash = node.get("content_hash", "")
        if label not in _CONFLICT_LABELS or not key:
            return None
        with neo4j_session(self.driver) as session:
            result = session.run(_LOOKUP_CYPHER, key=key, label=label)
            existing = result.single()
        if existing is None:
            return None
        old_hash = existing.get("content_hash")
        if old_hash == new_hash:
            return None
        conflict_type = _CONFLICT_TYPE_MAP.get(label, "definition_conflict")
        return ConflictEvent(
            graph="MG", node_id=key, conflict_type=conflict_type,
            candidates=[
                ConflictCandidate(node_id=existing["key"], confidence=0.5,
                                  evidence={"content_hash": old_hash, "side": "existing"}),
                ConflictCandidate(node_id=key, confidence=float(node.get("source_weight", 0.5)),
                                  evidence={"content_hash": new_hash, "side": "incoming"}),
            ],
            source="write",
        )

    def scan(self, since: Optional[dt.datetime] = None) -> list[ConflictEvent]:
        return []

    def apply(self, decision: ConflictDecision, event: ConflictEvent, winner_id: Optional[str] = None) -> None:
        if decision == ConflictDecision.NOOP:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        node_ids = list(dict.fromkeys(c.node_id for c in event.candidates))
        with neo4j_session(self.driver) as session:
            if decision == ConflictDecision.SUPERSEDE and winner_id:
                losers = [nid for nid in node_ids if nid != winner_id]
                if not losers:
                    log.info("Same-key SUPERSEDE for %s — no edge to write", winner_id)
                    return
                loser_id = losers[0]
                session.run(_MG_SUPERSEDE_CYPHER, old_key=loser_id, new_key=winner_id,
                            now=now, reason="arbiter_supersede")
            elif decision == ConflictDecision.CONTRADICTS and len(node_ids) >= 2:
                session.run(_MG_CONTRADICTS_CYPHER, key_a=node_ids[0], key_b=node_ids[1])

    def confidence(self, node_id: str) -> float:
        with neo4j_session(self.driver) as session:
            result = session.run("MATCH (n {key: $key}) RETURN n.source_weight AS w", key=node_id)
            row = result.single()
            return float(row["w"] or 0.5) if row else 0.5
