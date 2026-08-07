"""Knowledge Graph ingester（``graph_topology_v4.md`` 知识区）。

读取 ``data/test/external_events.yaml`` 写入：

- ``Entity``（跨图共享，``zone='_shared'``）
- ``Event``（holiday / model_release / promotion / outage 等，``zone='knowledge'``）

跨图边：

- ``Event -[:ABOUT]-> Entity``
- ``Entity -[:RELATED_TO]-> Entity``（``entity_links``；可选 ``sim_score``；写入时**同时** MERGE 反向边）
- ``Entity -[:SURFACE_METRIC]-> Metric``（``metrics_dict.entity_metric_surfaces``）

v4 不再写入 ``Policy`` / ``APPLIES_TO``、``Document`` / ``KnowledgeChunk``、``BRIDGES_TO``。
``policies`` / ``bridge_links`` 若出现在 YAML 中仅打日志并跳过（可迁到 Event 或外部存储）。

Event 写入走 :class:`KnowledgeWriter` 时补 provenance（``valid_at`` / ``ingest_at`` 等）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .knowledge_writer import KnowledgeWriter
from .semantic import load_metrics_dict

log = get_logger("graph.knowledge")


def _content_hash(obj: dict) -> str:
    """SHA-1 of stable JSON representation (sorted keys, truncated to 16 hex chars)."""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------- #
# 数据加载
# ---------------------------------------------------------------------- #
def load_external_events(path: Path) -> dict[str, Any]:
    """读 ``external_events.yaml``。"""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load external_events.yaml. Add `pyyaml>=6.0` to requirements.txt."
        ) from exc
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"external_events.yaml top-level must be a mapping, got {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------- #
# 写入入口
# ---------------------------------------------------------------------- #
def ingest_knowledge(
    driver: Driver,
    events_path: Path,
    *,
    embedder: Optional[Callable[[str], list[float]]] = None,
    metrics_dict_path: Optional[Path] = None,
) -> None:
    """读 YAML → Entity / Event 写入 Neo4j（v4：无 Policy / BRIDGES_TO）。

    Event 写入走 KnowledgeWriter 三决策流程（embedder 可选，None 时直接 ADD）。
    Entity 不走 KnowledgeWriter。

    ``metrics_dict_path``：若提供且存在，读取 ``entity_metric_surfaces``，写入
    ``Entity -[:SURFACE_METRIC]-> Metric``（须在语义层已写入 Metric 之后调用）。
    """
    data = load_external_events(events_path)
    entities = list(data.get("entities") or [])
    entity_links = list(data.get("entity_links") or [])
    bridge_links = list(data.get("bridge_links") or [])
    holidays = list(data.get("holidays") or [])
    releases = list(data.get("releases") or [])
    policies = list(data.get("policies") or [])

    surface_blocks: list[dict[str, Any]] = []
    if metrics_dict_path is not None and metrics_dict_path.exists():
        md = load_metrics_dict(metrics_dict_path)
        surface_blocks = list(md.get("entity_metric_surfaces") or [])

    log.info(
        "knowledge graph: entities=%d, entity_links=%d, bridge_links=%d, "
        "entity_metric_surfaces=%d, holidays=%d, releases=%d, policies_skipped=%d",
        len(entities),
        len(entity_links),
        len(bridge_links),
        len(surface_blocks),
        len(holidays),
        len(releases),
        len(policies),
    )

    kw = KnowledgeWriter(driver, embedder=embedder)

    merged_surface = 0
    attempted_surface = 0
    with neo4j_session(driver) as s:
        if entities:
            s.execute_write(_write_entities, entities=entities)
        if entity_links:
            s.execute_write(_write_entity_links, links=entity_links)
        if bridge_links:
            log.info(
                "knowledge graph: v4 skips %d bridge_links (no BRIDGES_TO); "
                "use explicit relations or app-level joins instead",
                len(bridge_links),
            )
        if surface_blocks:
            merged_surface, attempted_surface = s.execute_write(
                _write_entity_metric_surfaces_tx,
                blocks=surface_blocks,
            )

    # Events → KnowledgeWriter
    events_all = holidays + releases
    if events_all:
        _ingest_events_via_kw(kw, events_all, driver)

    if policies:
        log.warning(
            "knowledge graph: v4 ignores %d YAML policies entries "
            "(Policy nodes removed; encode as Event or external policy store)",
            len(policies),
        )

    if attempted_surface:
        log.info(
            "entity_metric_surfaces (SURFACE_METRIC): merged=%d attempted=%d",
            merged_surface,
            attempted_surface,
        )


def merge_topology_bridge_links(driver: Driver, links: list[dict]) -> tuple[int, int]:
    """v4: ``BRIDGES_TO`` 已从 schema 移除；``trace_bridge_links`` 仅记录日志。"""
    if not links:
        return 0, 0
    log.info(
        "merge_topology_bridge_links: v4 has no BRIDGES_TO edge; skipping %d link(s)",
        len(links),
    )
    return 0, len(links)


def _ingest_events_via_kw(kw: KnowledgeWriter, events: list[dict], driver: Driver) -> None:
    """Walk each event through KnowledgeWriter, then build ABOUT edges."""
    decision_counts: dict[str, int] = {}
    about_edges: list[tuple[str, str]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = str(ev.get("key") or "").strip()
        if not key:
            continue

        core = {k: v for k, v in ev.items() if k not in ("about_entity_key",)}
        fact = {
            "key": key,
            "label": "Event",
            "properties": {
                "type": str(ev.get("type") or ""),
                "scope": str(ev.get("scope") or "_global"),
                "name": str(ev.get("name") or ""),
                "description": str(ev.get("description") or ""),
                "date_from": str(ev.get("date_from") or ""),
                "date_to": str(ev.get("date_to") or ev.get("date_from") or ""),
            },
            "source_id": str(ev.get("source_id") or "legacy_etl"),
            "source_trust": float(ev.get("source_trust") or 0.8),
            "extractor": str(ev.get("extractor") or "rule"),
            "extractor_confidence": float(ev.get("extractor_confidence") or 0.8),
            "content_hash": ev.get("content_hash") or _content_hash(core),
            "ingest_method": str(ev.get("ingest_method") or "etl"),
        }
        decision = kw.write(fact)
        decision_counts[decision.value] = decision_counts.get(decision.value, 0) + 1

        about_key = str(ev.get("about_entity_key") or "").strip()
        if about_key:
            about_edges.append((key, about_key))

    log.info("events KnowledgeWriter decisions: %s", decision_counts)

    # Build ABOUT edges post-write (endpoints now exist)
    if about_edges:
        with neo4j_session(driver) as s:
            for ev_key, ent_key in about_edges:
                s.run(
                    """
                    MATCH (ev:Event {key: $ek})
                    OPTIONAL MATCH (ent:Entity {key: $ek2})
                    FOREACH (_ IN CASE WHEN ent IS NULL THEN [] ELSE [ent] END |
                        MERGE (ev)-[:ABOUT]->(ent)
                    )
                    """,
                    ek=ev_key,
                    ek2=ent_key,
                )


# ---------------------------------------------------------------------- #
# Entities (zone='_shared')
# ---------------------------------------------------------------------- #
def _write_entities(tx, *, entities: list[dict]) -> None:
    rows: list[dict] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        key = str(ent.get("key") or "").strip()
        if not key:
            continue
        rows.append(
            {
                "key": key,
                "type": str(ent.get("type") or ""),
                "canonical_name": str(ent.get("canonical_name") or ""),
                "aliases": [str(a) for a in (ent.get("aliases") or [])],
                "description": str(ent.get("description") or ""),
            }
        )
    if not rows:
        return
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (e:Entity {key: r.key})
          ON CREATE SET e.type = r.type, e.name = r.canonical_name, e.canonical_name = r.canonical_name,
                        e.aliases = r.aliases, e.description = r.description,
                        e.zone = '_shared'
          ON MATCH  SET e.type = r.type, e.name = r.canonical_name, e.canonical_name = r.canonical_name,
                        e.aliases = r.aliases, e.description = r.description,
                        e.zone = '_shared'
        """,
        rows=rows,
    )


def _write_entity_metric_surfaces_tx(tx, *, blocks: list[dict]) -> tuple[int, int]:
    """``entity_metric_surfaces`` → ``(:Entity)-[:SURFACE_METRIC]->(:Metric)``。"""
    merged = 0
    attempted = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        ek = str(block.get("entity_key") or "").strip()
        mp = str(block.get("merge_policy") or "")
        if not ek:
            continue
        for m in list(block.get("members") or []):
            if not isinstance(m, dict):
                continue
            mk = str(m.get("metric_key") or "").strip()
            if not mk:
                continue
            attempted += 1
            rec = tx.run(
                """
                MATCH (e:Entity {key: $ek})
                MATCH (met:Metric {key: $mk})
                MERGE (e)-[r:SURFACE_METRIC]->(met)
                  ON CREATE SET r.role = $role, r.notes = $notes,
                                r.merge_policy = $mp, r.zone = 'knowledge'
                  ON MATCH  SET r.role = $role, r.notes = $notes,
                                r.merge_policy = $mp, r.zone = 'knowledge'
                RETURN true AS ok
                LIMIT 1
                """,
                ek=ek,
                mk=mk,
                role=str(m.get("role") or "surface"),
                notes=str(m.get("notes") or ""),
                mp=mp,
            ).single()
            if rec:
                merged += 1
            else:
                log.warning("entity_metric_surface skipped: %s -> %s", ek, mk)
    return merged, attempted


def _write_entity_links(tx, *, links: list[dict]) -> None:
    """``entity_links`` → ``(:Entity)-[:RELATED_TO]->(:Entity)``（跨域拓扑）。

    每条输入边都会在 Neo4j 中落成**两条**有向 ``RELATED_TO``（``from→to`` 与 ``to→from``），
    属性镜像相同，便于只按出边或入边做遍历。
    """
    rows: list[dict[str, Any]] = []
    for raw in links:
        if not isinstance(raw, dict):
            continue
        fk = str(raw.get("from_key") or "").strip()
        tk = str(raw.get("to_key") or "").strip()
        if not fk or not tk or fk == tk:
            continue
        sim = raw.get("sim_score")
        try:
            sim_f = float(sim) if sim is not None and str(sim).strip() != "" else None
        except (TypeError, ValueError):
            sim_f = None
        rows.append(
            {
                "from_key": fk,
                "to_key": tk,
                "description": str(raw.get("description") or ""),
                "relation_subtype": str(raw.get("relation_subtype") or "cross_domain"),
                "scope": str(raw.get("scope") or ""),
                "sim_score": sim_f,
            }
        )
    if not rows:
        return
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (a:Entity {key: r.from_key})
        MATCH (b:Entity {key: r.to_key})
        WHERE a <> b
        MERGE (a)-[rel:RELATED_TO]->(b)
          ON CREATE SET rel.description = r.description,
                        rel.relation_subtype = r.relation_subtype,
                        rel.scope = r.scope,
                        rel.zone = 'knowledge',
                        rel.sim_score = CASE WHEN r.sim_score IS NULL THEN null ELSE r.sim_score END
          ON MATCH SET rel.description = r.description,
                       rel.relation_subtype = r.relation_subtype,
                       rel.scope = r.scope,
                       rel.sim_score = CASE WHEN r.sim_score IS NULL THEN rel.sim_score ELSE r.sim_score END
        MERGE (b)-[rel2:RELATED_TO]->(a)
          ON CREATE SET rel2.description = r.description,
                        rel2.relation_subtype = r.relation_subtype,
                        rel2.scope = r.scope,
                        rel2.zone = 'knowledge',
                        rel2.sim_score = CASE WHEN r.sim_score IS NULL THEN null ELSE r.sim_score END
          ON MATCH SET rel2.description = r.description,
                       rel2.relation_subtype = r.relation_subtype,
                       rel2.scope = r.scope,
                       rel2.sim_score = CASE WHEN r.sim_score IS NULL THEN rel2.sim_score ELSE r.sim_score END
        """,
        rows=rows,
    )


# ---------------------------------------------------------------------- #
# Events (zone='knowledge') + ABOUT 边
# ---------------------------------------------------------------------- #
def _write_events(tx, *, events: list[dict]) -> None:
    """v3: 补 provenance 字段（valid_at / ingest_at / source_id / source_trust / content_hash）。"""
    rows: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = str(ev.get("key") or "").strip()
        if not key:
            continue
        core = {k: v for k, v in ev.items() if k not in ("about_entity_key",)}
        rows.append(
            {
                "key": key,
                "type": str(ev.get("type") or ""),
                "scope": str(ev.get("scope") or "_global"),
                "name": str(ev.get("name") or ""),
                "description": str(ev.get("description") or ""),
                "date_from": str(ev.get("date_from") or ""),
                "date_to": str(ev.get("date_to") or ev.get("date_from") or ""),
                "about_entity_key": str(ev.get("about_entity_key") or "") or None,
                # v3 provenance defaults
                "source_id": str(ev.get("source_id") or "legacy_etl"),
                "source_trust": float(ev.get("source_trust") or 0.8),
                "content_hash": ev.get("content_hash") or _content_hash(core),
                "ingest_method": str(ev.get("ingest_method") or "etl"),
            }
        )
    if not rows:
        return
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (ev:Event {key: r.key})
          ON CREATE SET ev.type = r.type, ev.scope = r.scope, ev.name = r.name,
                        ev.description = r.description,
                        ev.date_from = CASE WHEN r.date_from = '' THEN null ELSE date(r.date_from) END,
                        ev.date_to   = CASE WHEN r.date_to   = '' THEN null ELSE date(r.date_to)   END,
                        ev.zone = 'knowledge',
                        ev.valid_at = datetime(),
                        ev.ingest_at = datetime(),
                        ev.source_id = r.source_id,
                        ev.source_trust = r.source_trust,
                        ev.content_hash = r.content_hash,
                        ev.ingest_method = r.ingest_method
          ON MATCH  SET ev.type = r.type, ev.scope = r.scope, ev.name = r.name,
                        ev.description = r.description,
                        ev.date_from = CASE WHEN r.date_from = '' THEN null ELSE date(r.date_from) END,
                        ev.date_to   = CASE WHEN r.date_to   = '' THEN null ELSE date(r.date_to)   END,
                        ev.zone = 'knowledge'
        WITH ev, r
        WHERE r.about_entity_key IS NOT NULL AND r.about_entity_key <> ''
        OPTIONAL MATCH (ent:Entity {key: r.about_entity_key})
        FOREACH (_ IN CASE WHEN ent IS NULL THEN [] ELSE [ent] END |
            MERGE (ev)-[:ABOUT]->(ent)
        )
        """,
        rows=rows,
    )


__all__ = [
    "KnowledgeWriter",
    "ingest_knowledge",
    "load_external_events",
]
