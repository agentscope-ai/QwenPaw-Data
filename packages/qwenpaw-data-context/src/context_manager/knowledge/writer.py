"""Write clustered facts + edges to Neo4j (KnowledgeWriter + semantic helpers)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from ..graph.keys import DEFAULT_DB_ID, DEFAULT_SCHEMA
from ..graph.knowledge import _write_entity_links
from ..graph.knowledge_writer import KnowledgeWriter, WriteDecision
from ..graph.profile import DatasetProfile
from ..utils import get_logger, neo4j_session
from .trust import source_trust

log = get_logger("knowledge.writer")

_KG_LIFECYCLE = frozenset(
    {
        "active",
        "archived",
        "frozen",
        "invalidated",
        "superseded",
        "needs_revalidation",
    }
)

_CORE_REL_TYPES = frozenset({
    "INSTANCE_OF", "DEVELOPED_BY", "OWNED_BY", "OFFERS",
    "HAS_FEATURE", "COMPETES_WITH", "SUCCEEDS", "USES_MODEL",
    "IMPACTS", "INVOLVES",
})


@dataclass
class IngestReport:
    decisions: list[tuple[str, str, str]] = field(default_factory=list)  # key, label, decision
    skipped: list[str] = field(default_factory=list)
    conflicts_noted: int = 0
    # Pass C kg_topology → Neo4j (write_kg_topology_v4); 0 if dry_run or no kg_topology writes
    pass_c_neo4j_entities: int = 0
    pass_c_neo4j_related_to_rows: int = 0
    pass_c_neo4j_has_instance_pairs: int = 0

    def count_by(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for _, _, d in self.decisions:
            c[d] = c.get(d, 0) + 1
        return c


def _merge_event_about(tx, *, ev_key: str, ent_key: str) -> None:
    tx.run(
        """
        MATCH (ev:Event {key: $ek})
        OPTIONAL MATCH (ent:Entity {key: $tk})
        FOREACH (_ IN CASE WHEN ent IS NULL THEN [] ELSE [ent] END |
            MERGE (ev)-[:ABOUT]->(ent)
        )
        """,
        ek=ev_key,
        tk=ent_key,
    )


def _merge_core_typed_rel(tx, *, from_key: str, to_key: str, rel_type: str, properties: dict) -> None:
    if rel_type not in _CORE_REL_TYPES:
        return
    safe_props = {k: v for k, v in properties.items() if isinstance(v, (str, int, float, bool))}
    tx.run(
        f"""
        MATCH (a {{key: $fk}})
        MATCH (b {{key: $tk}})
        MERGE (a)-[r:{rel_type}]->(b)
          ON CREATE SET r += $props, r.zone = 'knowledge'
        """,
        fk=from_key,
        tk=to_key,
        props=safe_props,
    )


def _merge_has_instance(tx, *, from_key: str, to_key: str) -> None:
    tx.run(
        """
        MATCH (a:Entity {key: $fk})
        MATCH (b:Entity {key: $tk})
        MERGE (a)-[r:HAS_INSTANCE]->(b)
          ON CREATE SET r.zone = 'knowledge'
          ON MATCH SET r.zone = coalesce(r.zone, 'knowledge')
        """,
        fk=from_key,
        tk=to_key,
    )


def write_kg_topology_v4(
    driver: Any,
    *,
    kg_topology: dict[str, Any],
    kw: KnowledgeWriter,
    rep: IngestReport,
    pass_c_confidence: float,
) -> None:
    """Apply Pass C ``kg_topology``: Entity nodes + RELATED_TO / HAS_INSTANCE only (no Event / ABOUT / CAUSES)."""
    if not isinstance(kg_topology, dict):
        return
    nodes = list(kg_topology.get("nodes") or [])
    rels = list(kg_topology.get("relationships") or [])
    if not nodes and not rels:
        return

    n_entity_writes = 0
    conf_f = max(0.05, min(1.0, float(pass_c_confidence)))

    for n in nodes:
        if not isinstance(n, dict):
            continue
        label = str(n.get("label") or "").strip()
        key = str(n.get("key") or "").strip()
        props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
        if label == "Entity" and key.startswith("ent:"):
            name = str(props.get("name") or key).strip()
            aliases_raw = props.get("aliases") or []
            if not isinstance(aliases_raw, list):
                aliases_raw = []
            aliases = [str(x).strip() for x in aliases_raw if str(x).strip()][:64]
            if not aliases:
                aliases = [name] if name else [key]
            desc = str(props.get("description") or "")[:4000]
            et = str(props.get("type") or "concept")[:120]
            ls = str(props.get("lifecycle_state") or "active").strip()
            if ls not in _KG_LIFECYCLE:
                ls = "active"

            # Resolve per-node source provenance from cluster metadata.
            # source_docs: sorted list of bare filenames (e.g. "report.pdf").
            # Falls back to "kg_topology" placeholder for nodes with no provenance.
            node_source_docs = [
                str(d).strip() for d in (n.get("source_docs") or []) if str(d).strip()
            ]
            primary_doc = node_source_docs[0] if node_source_docs else "kg_topology"
            primary_source_id = f"doc_ingest:{primary_doc}"

            tr = source_trust(
                source_doc=primary_doc,
                self_confidence=conf_f,
                repeat_count=1,
                is_mg_candidate=False,
            )

            fact = {
                "key": key,
                "label": "Entity",
                "graph_zone": "knowledge",
                "properties": {
                    "name": name,
                    "canonical_name": name,
                    "aliases": aliases,
                    "description": desc,
                    "type": et,
                    "lifecycle_state": ls,
                },
                "source_id": primary_source_id,
                "source_trust": tr,
                "extractor": "llm",
                "extractor_confidence": conf_f,
                "ingest_method": "agent_runtime",
            }
            dec = kw.write(fact)
            n_entity_writes += 1
            rep.decisions.append((key, "Entity", dec.value))

            # Register any additional source documents on the node so that
            # delete_kg_nodes_by_source can correctly track multi-doc provenance.
            for extra_doc in node_source_docs[1:]:
                kw._touch_last_seen(key, "Entity", source_id=f"doc_ingest:{extra_doc}")

    related_rows: list[dict[str, Any]] = []
    hasi_pairs: list[tuple[str, str]] = []
    core_typed_rels: list[tuple[str, str, str, dict]] = []

    for r in rels:
        if not isinstance(r, dict):
            continue
        rt = str(r.get("rel_type") or "").strip().upper()
        fk = str(r.get("from_key") or "").strip()
        tk = str(r.get("to_key") or "").strip()
        rp = r.get("properties") if isinstance(r.get("properties"), dict) else {}
        if rt == "RELATED_TO":
            if fk.startswith("ent:") and tk.startswith("ent:"):
                st = str(rp.get("relation_subtype") or rp.get("subtype") or "see_also")[:64]
                related_rows.append(
                    {
                        "from_key": fk,
                        "to_key": tk,
                        "description": str(rp.get("description") or "")[:500],
                        "relation_subtype": st,
                        "scope": str(rp.get("scope") or "")[:200],
                        "sim_score": rp.get("sim_score"),
                    }
                )
        elif rt == "HAS_INSTANCE" and fk.startswith("ent:") and tk.startswith("ent:"):
            hasi_pairs.append((fk, tk))
        elif rt in _CORE_REL_TYPES and (fk.startswith("ent:") or fk.startswith("ev:")):
            core_typed_rels.append((fk, tk, rt, rp))

    n_related_applied = min(len(related_rows), 5000) if related_rows else 0
    if related_rows:
        with neo4j_session(driver) as s:
            s.execute_write(_write_entity_links, links=related_rows[:5000])

    if hasi_pairs or core_typed_rels:

        def _apply_kg_rels(tx: Any) -> None:
            for fk, tk in hasi_pairs:
                _merge_has_instance(tx, from_key=fk, to_key=tk)
            for fk, tk, rt, rp in core_typed_rels:
                _merge_core_typed_rel(tx, from_key=fk, to_key=tk, rel_type=rt, properties=rp)

        with neo4j_session(driver) as s:
            s.execute_write(_apply_kg_rels)

    rep.pass_c_neo4j_entities = n_entity_writes
    rep.pass_c_neo4j_related_to_rows = n_related_applied
    rep.pass_c_neo4j_has_instance_pairs = len(hasi_pairs) + len(core_typed_rels)


def ingest_all(
    driver: Optional[Any],
    *,
    pass_a_flat: list[dict[str, Any]],
    surface_to_canonical: dict[str, str],
    embedder,
    profile: Optional[DatasetProfile],
    db_id: str = DEFAULT_DB_ID,
    schema: str = DEFAULT_SCHEMA,
    dry_run: bool = False,
    kg_topology: Optional[dict[str, Any]] = None,
    pass_c_confidence: float = 0.75,
) -> IngestReport:
    """Merge graph facts: Pass C ``kg_topology`` entities + kg edges first, then Events/ABOUT, Pass A rels, semantics.

    Canonical ``ent:`` nodes and RELATED_TO / HAS_INSTANCE from Pass C only (Pass B clusters are input to Pass C,
    not written here as standalone entities).
    """
    rep = IngestReport()
    if dry_run:
        rep.skipped.append("dry_run: no Neo4j writes")
        return rep
    if driver is None:
        raise ValueError("ingest_all requires Neo4j driver when dry_run is False")

    from ..conflict.screener import ConflictScreener

    screener = ConflictScreener() if embedder else None
    kw = KnowledgeWriter(driver, embedder=embedder, conflict_screener=screener)

    # ---- Pass C: kg_topology Entity nodes + RELATED_TO / HAS_INSTANCE (before Events so ABOUT resolves) ----
    write_kg_topology_v4(
        driver,
        kg_topology=kg_topology if isinstance(kg_topology, dict) else {},
        kw=kw,
        rep=rep,
        pass_c_confidence=pass_c_confidence,
    )

    # ---- Events ----
    ev_digest_seen: set[str] = set()
    for row in pass_a_flat:
        doc = str(row.get("_source_doc") or "__merged__")
        for ev in row.get("events") or []:
            if not isinstance(ev, dict):
                continue
            name = str(ev.get("name") or "").strip()
            if not name:
                continue
            digest = hashlib.sha1(
                f"{name}|{ev.get('date_from')}|{ev.get('about_surface')}".encode()
            ).hexdigest()[:16]
            if digest in ev_digest_seen:
                continue
            ev_digest_seen.add(digest)
            ev_key = f"ev:doc:{digest}"
            tr = source_trust(
                source_doc=doc,
                self_confidence=float(row.get("self_confidence") or 0.7),
                repeat_count=1,
                is_mg_candidate=False,
            )
            fact = {
                "key": ev_key,
                "label": "Event",
                "properties": {
                    "name": name[:500],
                    "type": str(ev.get("type") or "other"),
                    "description": str(ev.get("description") or "")[:4000],
                    "date_from": str(ev.get("date_from") or "")[:32],
                    "date_to": str(ev.get("date_to") or "")[:32],
                },
                "source_id": f"doc_ingest:{doc}",
                "source_trust": tr,
                "extractor": "llm",
                "extractor_confidence": float(row.get("self_confidence") or 0.7),
                "ingest_method": "agent_runtime",
            }
            dec = kw.write(fact)
            rep.decisions.append((ev_key, "Event", dec.value))
            about = str(ev.get("about_surface") or "").strip()
            ent_key = surface_to_canonical.get(about, "")
            if ent_key:
                with neo4j_session(driver) as s:
                    s.execute_write(_merge_event_about, ev_key=ev_key, ent_key=ent_key)

    # ---- RELATED_TO from entity_relations ----
    link_rows: list[dict[str, Any]] = []
    for row in pass_a_flat:
        for rel in row.get("entity_relations") or []:
            if not isinstance(rel, dict):
                continue
            fk = surface_to_canonical.get(str(rel.get("from_surface") or "").strip(), "")
            tk = surface_to_canonical.get(str(rel.get("to_surface") or "").strip(), "")
            if not fk or not tk:
                continue
            link_rows.append(
                {
                    "from_key": fk,
                    "to_key": tk,
                    "description": str(rel.get("description") or "")[:500],
                    "relation_subtype": str(rel.get("relation_subtype") or "see_also")[:64],
                    "scope": str(rel.get("scope") or "")[:200],
                    "sim_score": None,
                }
            )
    if link_rows:
        with neo4j_session(driver) as s:
            s.execute_write(_write_entity_links, links=link_rows[:5000])

    return rep
