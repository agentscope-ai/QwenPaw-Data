"""KnowledgeWriter — 知识库统一写入入口。

写入流程（三层）：
  Layer 0  同 key 命中 → NOOP（hash 相同）或 UPDATE（hash 不同）
  Layer 1  RRF 混合检索（fulltext + vector）粗筛近似节点
  Layer 2  LLM 裁决（仅 rrf_score ≥ 阈值时触发）→ MERGE / CONFLICT / CLEAR
  fallback ANN 近邻探测：ADD（+ 可选 RELATED_TO 边）

Layer 1+2 由 ``conflict.screener.ConflictScreener`` 提供，可选注入。
未注入时退化为原有行为（Layer 0 + ANN）。

外部使用：

.. code-block:: python

    writer = KnowledgeWriter(driver, embedder=embed_one)
    decision = writer.write(fact)
    # decision ∈ WriteDecision enum

``fact`` dict 必填字段（其余为 optional provenance）：

.. code-block:: python

    fact = {
        "key":          "ev:holiday_cn_2026_spring_festival",  # 节点唯一 key
        "label":        "Event",                                # 节点 label
        "properties":   {                                       # 节点属性 dict
            "name": "2026 春节",
            "type": "holiday",
            "date_from": "2026-01-28",
            "date_to":   "2026-02-03",
        },
        # 可选 provenance（缺失时填默认值）
        "graph_zone": "knowledge",           # 可选；Entity 用 ``_shared``，默认 ``knowledge``
        "source_id":    "etl:holiday_cn",
        "source_trust": 0.95,
        "extractor":    "rule",            # rule | llm | manual | tool
        "extractor_confidence": 0.95,
        "ingest_method": "etl",            # etl | agent_runtime | user_correction
        "valid_at":     "2026-01-01",      # fact event time (ISO); default now()
    }
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from enum import Enum
from typing import Any, Callable, Optional

from neo4j import Driver

from ..config import CFG
from ..utils import get_logger, neo4j_session

log = get_logger("graph.knowledge_writer")

# Log missing Neo4j vector index once per index name (avoids spam during ingest before init_vector_indexes).
_MISSING_VEC_INDEX_LOGGED: set[str] = set()


# ---------------------------------------------------------------------- #
# WriteDecision enum
# ---------------------------------------------------------------------- #

class WriteDecision(str, Enum):
    NOOP = "NOOP"
    UPDATE = "UPDATE"
    ADD = "ADD"


# ---------------------------------------------------------------------- #
# Internal constants
# ---------------------------------------------------------------------- #

# ANN similarity threshold for RELATED_TO edge
_ANN_RELATED_THRESHOLD = 0.60

# Default ANN oversample for neighbor search
_ANN_K = 3

# Supported dual-temporal labels (§5)
_BITEMPORAL_LABELS = frozenset({"Claim", "Formula", "Caliber", "Event", "Strategy"})


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _content_hash(properties: dict) -> str:
    """SHA-1 of stable JSON representation (truncated to 16 hex chars)."""
    payload = json.dumps(properties, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _default_provenance(fact: dict) -> dict:
    """Fill missing provenance fields with safe defaults."""
    props = dict(fact.get("properties") or {})
    prov = {
        "source_id": fact.get("source_id") or "legacy_etl",
        "source_trust": float(fact.get("source_trust") or 0.8),
        "extractor": fact.get("extractor") or "rule",
        "extractor_confidence": float(fact.get("extractor_confidence") or 0.8),
        "content_hash": fact.get("content_hash") or _content_hash(props),
        "ingest_method": fact.get("ingest_method") or "etl",
        "valid_at": fact.get("valid_at") or _now_iso(),
        "ingest_at": _now_iso(),
    }
    return prov


# ---------------------------------------------------------------------- #
# KnowledgeWriter
# ---------------------------------------------------------------------- #

class KnowledgeWriter:
    """知识库统一写入入口（§4.3 三决策 + 分层冲突检测）。

    Args:
        driver:  Neo4j driver.
        embedder:  ``(text: str) -> list[float]`` callable for ANN neighbor search.
                   If None, ANN step is skipped (direct ADD).
        ann_index_map:  Map of label → vector index name for ANN queries.
                        Defaults to ``{"Metric": "met_vec", "Column": "col_vec", ...}`` plus
                        ``Event`` / ``Entity`` → ``ev_vec`` / ``ent_vec`` when present in Neo4j.
        conflict_screener:  Optional ``ConflictScreener`` instance for Layer 1+2
                            conflict detection. If None, conflict screening is skipped.
    """

    _DEFAULT_ANN_INDEXES: dict[str, str] = {
        "Metric": "met_vec",
        "Column": "col_vec",
        "Dimension": "dim_vec",
        "Event": "ev_vec",
        "Entity": "ent_vec",
    }

    def __init__(
        self,
        driver: Driver,
        embedder: Optional[Callable[[str], list[float]]] = None,
        ann_index_map: Optional[dict[str, str]] = None,
        conflict_screener: Optional[Any] = None,
    ) -> None:
        self.driver = driver
        self.embedder = embedder
        self.ann_index_map = ann_index_map or self._DEFAULT_ANN_INDEXES
        self.conflict_screener = conflict_screener

    # ------------------------------------------------------------------ #
    # Main entry
    # ------------------------------------------------------------------ #

    def write(self, fact: dict) -> WriteDecision:
        """三决策主入口。

        Args:
            fact: See module docstring for required fields.

        Returns:
            WriteDecision enum value.
        """
        key = str(fact.get("key") or "").strip()
        label = str(fact.get("label") or "").strip()
        properties: dict = dict(fact.get("properties") or {})

        if not key or not label:
            log.warning("KnowledgeWriter.write: missing key or label, skipping")
            return WriteDecision.NOOP

        prov = _default_provenance(fact)
        new_hash = prov["content_hash"]

        # ---- Decision 1: same key ----
        existing = self._lookup_by_key(key, label)
        if existing is not None:
            if existing.get("content_hash") == new_hash:
                self._touch_last_seen(key, label, source_id=prov.get("source_id", ""))
                log.debug("NOOP: %s (same hash)", key)
                return WriteDecision.NOOP
            else:
                self._update_node(key, label, properties, prov)
                log.info("UPDATE: %s (hash changed)", key)
                if label in ("Entity", "Event"):
                    self._sync_kb_embedding(key, label, properties)
                return WriteDecision.UPDATE

        # ---- Layer 1+2: RRF conflict screening (Entity / Event only) ----
        _conflict_peer: Optional[str] = None
        if label in ("Entity", "Event") and self.conflict_screener:
            result = self.conflict_screener.screen_and_judge(
                self.driver, label, properties, exclude_key=key,
            )
            if result.action == "MERGE":
                self._update_node(result.hit.key, label, properties, prov)
                if label in ("Entity", "Event"):
                    self._sync_kb_embedding(result.hit.key, label, properties)
                log.info(
                    "MERGE: %s → %s (rrf=%.3f, verdict=%s)",
                    key, result.hit.key, result.hit.rrf_score,
                    result.verdict.reason if result.verdict else "",
                )
                return WriteDecision.UPDATE
            elif result.action == "CONFLICT":
                _conflict_peer = result.hit.key
                log.warning(
                    "CONFLICT: %s ↔ %s (rrf=%.3f): %s",
                    key, result.hit.key, result.hit.rrf_score,
                    result.verdict.reason if result.verdict else "",
                )
            elif result.action == "MAYBE_DUP":
                log.warning(
                    "MAYBE_DUP: %s ↔ %s (rrf=%.3f)",
                    key, result.hit.key, result.hit.rrf_score,
                )

        # ---- Decision 2: ANN neighbor search (RELATED_TO only) ----
        neighbor: Optional[dict] = None

        if self.embedder:
            text = self._fact_text(label, properties)
            emb = self.embedder(text)
            fact_type = str(properties.get("type") or "").strip()
            neighbors = self._ann_neighbors(label, emb, k=_ANN_K, fact_type=fact_type)
            if neighbors:
                top = neighbors[0]
                cos_sim = float(top.get("score") or 0.0)
                if cos_sim >= _ANN_RELATED_THRESHOLD:
                    neighbor = top

        # ---- ADD ----
        self._add_node(
            key,
            label,
            properties,
            prov,
            graph_zone=str(fact.get("graph_zone") or "").strip() or None,
        )
        if neighbor and self.embedder:
            self._add_related_to(key, label, neighbor["key"])
        if label in ("Entity", "Event"):
            self._sync_kb_embedding(key, label, properties)
        if _conflict_peer:
            self._write_contradicts(key, _conflict_peer)
        log.info("ADD: %s (%s)", key, label)
        return WriteDecision.ADD

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _kb_embedding_text(self, label: str, properties: dict) -> str:
        """Prefer ``description`` (aligned with description-only fulltext index); else fact text."""
        desc = str(properties.get("description") or "").strip()
        if desc:
            return desc
        return (self._fact_text(label, properties) or "").strip()

    def _sync_kb_embedding(self, key: str, label: str, properties: dict) -> None:
        """Write ``embedding`` + ``embedding_hash`` for :Event / :Entity (needs ``ev_vec`` / ``ent_vec``)."""
        if label not in ("Entity", "Event") or not self.embedder:
            return
        text = self._kb_embedding_text(label, properties)
        if not text:
            return
        try:
            vec = self.embedder(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_kb_embedding embed failed key=%s: %s", key, exc)
            return
        h = hashlib.sha1()
        h.update(str(CFG.embed_model).encode("utf-8"))
        h.update(b"\x1f")
        h.update(text.encode("utf-8"))
        emb_hash = h.hexdigest()
        try:
            with neo4j_session(self.driver) as s:
                s.run(
                    f"MATCH (n:{label} {{key: $key}}) SET n.embedding = $vec, n.embedding_hash = $h",
                    key=key,
                    vec=vec,
                    h=emb_hash,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_kb_embedding SET failed key=%s: %s", key, exc)

    def _lookup_by_key(self, key: str, label: str) -> Optional[dict]:
        with neo4j_session(self.driver) as s:
            rec = s.run(
                f"MATCH (n:{label} {{key: $key}}) "
                "RETURN n.content_hash AS content_hash, n.valid_to AS valid_to, "
                "n.source_trust AS source_trust, n.valid_at AS valid_at",
                key=key,
            ).single()
        if rec is None:
            return None
        return dict(rec)

    def _touch_last_seen(self, key: str, label: str, source_id: str = "") -> None:
        """Update last_seen_at and append source_id to source_ids list (if provided)."""
        with neo4j_session(self.driver) as s:
            s.run(
                f"""
                MATCH (n:{label} {{key: $key}})
                SET n.last_seen_at = datetime(),
                    n.source_ids = CASE
                      WHEN $src = '' THEN coalesce(n.source_ids, [n.source_id])
                      WHEN n.source_ids IS NULL THEN [coalesce(n.source_id, $src), $src]
                      WHEN $src IN n.source_ids THEN n.source_ids
                      ELSE n.source_ids + [$src]
                    END
                """,
                key=key,
                src=source_id,
            )

    def _update_node(
        self, key: str, label: str, properties: dict, prov: dict
    ) -> None:
        """Update existing node attributes (MERGE ON MATCH). Provenance is refreshed.

        ``source_ids`` is treated as a growing list: the new source_id is appended
        rather than overwriting, so multi-document provenance is preserved.
        """
        set_parts = {**properties, **prov}
        # Remove temporal fields that should not be overwritten on update.
        set_parts.pop("valid_at", None)
        set_parts["ingest_at"] = prov["ingest_at"]
        set_parts["content_hash"] = prov["content_hash"]
        # source_ids is maintained as a list — remove it from the flat SET and handle separately.
        new_source_id = set_parts.pop("source_id", "") or ""
        set_parts.pop("source_ids", None)
        with neo4j_session(self.driver) as s:
            s.run(
                f"""
                MATCH (n:{label} {{key: $key}})
                SET n += $props,
                    n.source_ids = CASE
                      WHEN n.source_ids IS NULL THEN [coalesce(n.source_id, $src), $src]
                      WHEN $src IN n.source_ids THEN n.source_ids
                      ELSE n.source_ids + [$src]
                    END,
                    n.source_id = $src
                """,
                key=key,
                props=set_parts,
                src=new_source_id,
            )

    def _add_node(
        self, key: str, label: str, properties: dict, prov: dict, *, graph_zone: str | None = None
    ) -> None:
        """Create new node with MERGE (idempotent on key).

        ``source_ids`` is initialised to ``[source_id]`` on CREATE; on MATCH
        (race / re-ingest) the new source_id is appended to the list.
        """
        zone = (graph_zone or "").strip() or "knowledge"
        all_props: dict[str, Any] = {
            "key": key,
            **properties,
            **prov,
            "zone": zone,
        }
        # Convert valid_at to ISO string for datetime() Cypher call
        if isinstance(all_props.get("valid_at"), _dt.datetime):
            all_props["valid_at"] = all_props["valid_at"].isoformat()

        source_id = str(all_props.get("source_id") or "")
        # Exclude source_ids from the flat props dict — handled via dedicated expression.
        flat_props = {
            k: v for k, v in all_props.items()
            if k not in ("valid_at", "ingest_at", "source_ids")
        }

        with neo4j_session(self.driver) as s:
            s.run(
                f"""
                MERGE (n:{label} {{key: $key}})
                ON CREATE SET n += $props,
                              n.valid_at = datetime($valid_at),
                              n.ingest_at = datetime($ingest_at),
                              n.source_ids = [$src]
                ON MATCH  SET n += $props,
                              n.source_ids = CASE
                                WHEN n.source_ids IS NULL THEN [coalesce(n.source_id, $src), $src]
                                WHEN $src IN n.source_ids THEN n.source_ids
                                ELSE n.source_ids + [$src]
                              END
                """,
                key=key,
                props=flat_props,
                valid_at=str(all_props.get("valid_at") or _now_iso()),
                ingest_at=str(all_props.get("ingest_at") or _now_iso()),
                src=source_id,
            )

    def _ann_neighbors(
        self, label: str, emb: list[float], k: int = _ANN_K, *, fact_type: str = ""
    ) -> list[dict]:
        index_name = self.ann_index_map.get(label)
        if not index_name:
            return []
        type_filter = "AND n.type = $fact_type" if fact_type else ""
        cypher = f"""
        CALL db.index.vector.queryNodes('{index_name}', $k, $emb) YIELD node AS n, score
        WHERE (n.valid_to IS NULL OR n.valid_to > datetime())
        {type_filter}
        RETURN n.key AS key, n.content_hash AS content_hash,
               n.valid_at AS valid_at, n.source_trust AS source_trust,
               coalesce(n.name, '') AS name,
               coalesce(n.canonical_name, '') AS canonical_name,
               score
        LIMIT $k
        """
        try:
            with neo4j_session(self.driver) as s:
                return s.run(cypher, k=k, emb=emb, fact_type=fact_type).data()
        except Exception as exc:
            err = str(exc).lower()
            if "no such vector schema index" in err or "no such vector index" in err:
                if index_name not in _MISSING_VEC_INDEX_LOGGED:
                    _MISSING_VEC_INDEX_LOGGED.add(index_name)
                    log.warning(
                        "Vector index %r missing — ANN skipped for label=%s. "
                        "Create it with context_manager.graph.schema_init.init_vector_indexes(driver) "
                        "(or init_all) on this Neo4j database.",
                        index_name,
                        label,
                    )
                else:
                    log.debug("ANN skipped: index %r still missing", index_name)
                return []
            log.warning("ANN neighbor search failed for label=%s: %s", label, exc)
            return []

    def _add_related_to(self, new_key: str, label: str, neighbor_key: str) -> None:
        """Create RELATED_TO edges (bidirectional) for moderate-similarity neighbors."""
        with neo4j_session(self.driver) as s:
            s.run(
                f"""
                MATCH (n:{label} {{key: $nk}})
                OPTIONAL MATCH (o {{key: $ok}})
                WITH n, o
                WHERE o IS NOT NULL AND n <> o
                MERGE (n)-[:RELATED_TO]->(o)
                MERGE (o)-[:RELATED_TO]->(n)
                """,
                nk=new_key,
                ok=neighbor_key,
            )

    def _write_contradicts(self, key_a: str, key_b: str) -> None:
        """Create bidirectional CONTRADICTS edges between two conflicting nodes."""
        try:
            with neo4j_session(self.driver) as s:
                s.run(
                    "MATCH (a {key: $ka}), (b {key: $kb}) "
                    "MERGE (a)-[:CONTRADICTS]->(b) "
                    "MERGE (b)-[:CONTRADICTS]->(a)",
                    ka=key_a,
                    kb=key_b,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("_write_contradicts failed %s ↔ %s: %s", key_a, key_b, exc)

    @staticmethod
    def _fact_text(label: str, properties: dict) -> str:
        """Build a short text from key properties for embedding."""
        parts = []
        for field in ("name", "canonical_name", "description", "type", "text"):
            val = properties.get(field)
            if val:
                parts.append(str(val))
        return " ".join(parts) or label


__all__ = [
    "KnowledgeWriter",
    "WriteDecision",
]
