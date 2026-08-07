"""KG conflict adapter — bridges KnowledgeWriter with conflict protocol."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from neo4j import Driver

from ...utils import get_logger, neo4j_session
from ..model import ConflictCandidate, ConflictDecision, ConflictEvent, ConflictType
from ..schema import factual_diff_check

log = get_logger("conflict.adapters.kg")

_LOOKUP_CYPHER = (
    "MATCH (n {key: $key}) "
    "WHERE $label IN labels(n) "
    "RETURN n.key AS key, $label AS label, n.content_hash AS content_hash, "
    "n.type AS type, properties(n) AS props"
)

_SUPERSEDE_CYPHER = (
    "MATCH (old {key: $old_key}), (new {key: $new_key}) "
    "SET old.valid_to = datetime($now) "
    "MERGE (old)-[r:SUPERSEDED_BY]->(new) "
    "SET r.reason = $reason, r.decided_at = datetime($now), r.queue_id = $queue_id"
)

_CONTRADICTS_CYPHER = (
    "MATCH (a {key: $key_a}), (b {key: $key_b}) "
    "MERGE (a)-[r1:CONTRADICTS]->(b) "
    "SET r1.queue_id = $queue_id "
    "MERGE (b)-[r2:CONTRADICTS]->(a) "
    "SET r2.queue_id = $queue_id"
)


class KGConflictAdapter:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def detect(self, node: dict) -> Optional[ConflictEvent]:
        key = str(node.get("key") or "").strip()
        label = str(node.get("label") or "").strip()
        new_hash = node.get("content_hash") or self._compute_hash(node)
        if not key or not label:
            return None
        with neo4j_session(self.driver) as session:
            result = session.run(_LOOKUP_CYPHER, key=key, label=label)
            existing = result.single()
        if existing is None:
            return None
        old_hash = existing.get("content_hash")
        if old_hash == new_hash:
            return None

        new_props = dict(node.get("properties") or {})
        old_props = dict(existing.get("props") or {})
        node_type = str(new_props.get("type") or existing.get("type") or "")

        diff = factual_diff_check(label, node_type, old_props, new_props)
        if diff is None:
            return None
        if diff.normalized_equal:
            return None

        new_trust = float(node.get("source_trust", 0.5))
        return ConflictEvent(
            graph="KG", node_id=key,
            conflict_type=ConflictType.C1_FACTUAL,
            candidates=[
                ConflictCandidate(
                    node_id=existing["key"], confidence=0.5,
                    evidence={"content_hash": old_hash, "side": "existing",
                              "factual_values": {p: v[0] for p, v in diff.changed_props.items()}},
                ),
                ConflictCandidate(
                    node_id=key, confidence=new_trust,
                    evidence={"content_hash": new_hash, "side": "incoming",
                              "source_trust": new_trust,
                              "factual_values": {p: v[1] for p, v in diff.changed_props.items()}},
                ),
            ],
            source="write",
        )

    def scan(self, since: Optional[dt.datetime] = None) -> list[ConflictEvent]:
        """Scan KG for semantic conflicts via ANN similarity.

        For each active node with an embedding, search for near-duplicate neighbors
        (cos_sim >= 0.92) that have different content_hash. Deduplicates pairs.
        """
        # Label → vector index name (same as KnowledgeWriter defaults)
        _ANN_INDEXES = {
            "Event": "ev_vec",
            "Entity": "ent_vec",
        }

        _SCAN_NODES_CYPHER = (
            "MATCH (n:{label}) "
            "WHERE n.embedding IS NOT NULL "
            "  AND (n.valid_to IS NULL OR n.valid_to > datetime()) "
            "{since_clause} "
            "RETURN n.key AS key, n.content_hash AS content_hash, "
            "       n.embedding AS embedding, n.source_trust AS source_trust"
        )

        _ANN_QUERY = (
            "CALL db.index.vector.queryNodes('{index_name}', $k, $emb) "
            "YIELD node AS n, score "
            "WHERE score >= $threshold AND n.key <> $exclude_key "
            "  AND (n.valid_to IS NULL OR n.valid_to > datetime()) "
            "RETURN n.key AS key, n.content_hash AS content_hash, "
            "       n.source_trust AS source_trust, score"
        )

        threshold = 0.92
        k = 5
        events: list[ConflictEvent] = []
        seen_pairs: set[tuple[str, str]] = set()

        since_clause = "AND n.updated_at > datetime($since)" if since else ""
        since_param = since.isoformat() if since else None

        for label, index_name in _ANN_INDEXES.items():
            cypher = _SCAN_NODES_CYPHER.format(label=label, since_clause=since_clause)
            try:
                with neo4j_session(self.driver) as session:
                    nodes = session.run(cypher, since=since_param).data()
            except Exception as exc:
                log.warning("KG scan %s: query failed: %s", label, exc)
                continue

            if not nodes:
                continue

            log.info("KG scan %s: %d nodes with embeddings", label, len(nodes))

            for node in nodes:
                emb = node.get("embedding")
                if not emb:
                    continue

                try:
                    with neo4j_session(self.driver) as session:
                        ann_cypher = _ANN_QUERY.format(index_name=index_name)
                        neighbors = session.run(
                            ann_cypher, k=k, emb=emb, threshold=threshold,
                            exclude_key=node["key"],
                        ).data()
                except Exception as exc:
                    err = str(exc).lower()
                    if "no such vector" in err:
                        log.debug("KG scan %s: vector index %s missing, skip", label, index_name)
                    else:
                        log.warning("KG scan %s ANN failed: %s", label, exc)
                    continue

                for neighbor in neighbors:
                    # Same content_hash = same fact, not a conflict
                    if neighbor.get("content_hash") == node.get("content_hash"):
                        continue

                    # Deduplicate: only report each pair once
                    pair = tuple(sorted([node["key"], neighbor["key"]]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    events.append(ConflictEvent(
                        graph="KG",
                        node_id=node["key"],
                        conflict_type=ConflictType.C2_DUPLICATE,
                        candidates=[
                            ConflictCandidate(
                                node_id=node["key"],
                                confidence=float(node.get("source_trust") or 0.5),
                                evidence={"content_hash": node.get("content_hash"),
                                          "side": "scan_a"},
                            ),
                            ConflictCandidate(
                                node_id=neighbor["key"],
                                confidence=float(neighbor.get("source_trust") or 0.5),
                                evidence={"content_hash": neighbor.get("content_hash"),
                                          "side": "scan_b",
                                          "ann_score": neighbor.get("score")},
                            ),
                        ],
                        source="scan",
                    ))

        log.info("KG scan complete: %d conflicts found", len(events))
        return events

    def apply(self, decision: ConflictDecision, event: ConflictEvent, winner_id: Optional[str] = None) -> None:
        if decision == ConflictDecision.NOOP:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        node_ids = list(dict.fromkeys(c.node_id for c in event.candidates))  # deduplicate, preserve order
        with neo4j_session(self.driver) as session:
            if decision == ConflictDecision.SUPERSEDE and winner_id:
                losers = [nid for nid in node_ids if nid != winner_id]
                if not losers:
                    # Same-key conflict: writer updates in place, no SUPERSEDED_BY edge needed
                    log.info("Same-key SUPERSEDE for %s — no edge to write", winner_id)
                    return
                loser_id = losers[0]
                session.run(_SUPERSEDE_CYPHER, old_key=loser_id, new_key=winner_id,
                            now=now, reason="arbiter_supersede", queue_id="")
            elif decision == ConflictDecision.CONTRADICTS and len(node_ids) >= 2:
                session.run(_CONTRADICTS_CYPHER, key_a=node_ids[0], key_b=node_ids[1], queue_id="")

    def confidence(self, node_id: str) -> float:
        with neo4j_session(self.driver) as session:
            result = session.run("MATCH (n {key: $key}) RETURN n.source_trust AS trust", key=node_id)
            row = result.single()
            return float(row["trust"] or 0.5) if row else 0.5

    @staticmethod
    def _compute_hash(node: dict) -> str:
        props = json.dumps(node.get("properties", {}), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(props.encode()).hexdigest()[:16]
