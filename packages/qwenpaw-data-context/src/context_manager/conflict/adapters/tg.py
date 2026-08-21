"""TG conflict adapter — StrategyCard + Claim conflicts."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from neo4j import Driver

from ...utils import get_logger, neo4j_session
from ..model import ConflictCandidate, ConflictDecision, ConflictEvent

log = get_logger("conflict.adapters.tg")

_STRATEGY_LOOKUP_CYPHER = (
    "MATCH (n:Strategy {pattern_hash: $pattern_hash}) "
    "WHERE n.key <> $exclude_key "
    "RETURN n.key AS key, n.polarity AS polarity, n.success_rate AS success_rate "
    "LIMIT 10"
)

_TG_SUPERSEDE_CYPHER = (
    "MATCH (old {key: $old_key}), (new {key: $new_key}) "
    "SET old.valid_to = datetime($now) "
    "MERGE (old)-[r:SUPERSEDED_BY]->(new) "
    "SET r.reason = $reason, r.decided_at = datetime($now)"
)

_TG_CONTRADICTS_CYPHER = (
    "MATCH (a {key: $key_a}), (b {key: $key_b}) "
    "MERGE (a)-[r1:CONTRADICTS]->(b) "
    "MERGE (b)-[r2:CONTRADICTS]->(a)"
)


class TGConflictAdapter:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def detect(self, node: dict) -> Optional[ConflictEvent]:
        label = node.get("label", "")
        if label == "Strategy":
            return self._detect_strategy_conflict(node)
        return None

    def _detect_strategy_conflict(self, node: dict) -> Optional[ConflictEvent]:
        pattern_hash = node.get("pattern_hash", "")
        new_polarity = node.get("polarity", "")
        new_key = node.get("key", "")
        new_rate = float(node.get("success_rate", 0.0))
        if not pattern_hash or not new_polarity:
            return None
        with neo4j_session(self.driver) as session:
            result = session.run(_STRATEGY_LOOKUP_CYPHER, pattern_hash=pattern_hash, exclude_key=new_key)
            existing = [dict(r) for r in result]
        for ex in existing:
            if ex.get("polarity") and ex["polarity"] != new_polarity:
                return ConflictEvent(
                    graph="TG", node_id=new_key, conflict_type="strategy_polarity",
                    candidates=[
                        ConflictCandidate(node_id=ex["key"], confidence=float(ex.get("success_rate", 0.5)),
                                          evidence={"polarity": ex["polarity"]}),
                        ConflictCandidate(node_id=new_key, confidence=new_rate,
                                          evidence={"polarity": new_polarity}),
                    ],
                    source="write",
                )
        return None

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
                session.run(_TG_SUPERSEDE_CYPHER, old_key=loser_id, new_key=winner_id,
                            now=now, reason="arbiter_supersede")
            elif decision == ConflictDecision.CONTRADICTS and len(node_ids) >= 2:
                session.run(_TG_CONTRADICTS_CYPHER, key_a=node_ids[0], key_b=node_ids[1])

    def confidence(self, node_id: str) -> float:
        with neo4j_session(self.driver) as session:
            result = session.run("MATCH (n {key: $key}) RETURN n.success_rate AS rate", key=node_id)
            row = result.single()
            return float(row["rate"] or 0.5) if row else 0.5
