"""Context session operations used by the unified CM API."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Literal, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .ctx_assemble import (
    EntityDetail,
    ExperienceFragment,
    ExperienceStats,
    SupersedeChain,
    assemble_entity_detail_from_expand,
)
from .ctx_session import (
    ContextSession,
    OutcomeRecord,
    SessionStore,
    make_snapshot,
)

log = logging.getLogger("api.ctx_service")

def _get_driver(request: Request):
    return request.app.state.driver


def _get_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def _require_session(session_ref: str, store: SessionStore) -> ContextSession:
    session = store.get(session_ref)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session_ref '{session_ref}' not found or expired")
    return session


def _db_id_from_session(session: Optional[ContextSession]) -> str:
    """Derive primary_db_id from session.datasource_id via the registry."""
    if session is None:
        return ""
    ds_id = (session.datasource_id or "").strip()
    if not ds_id:
        return ""
    from ..graph.datasource_registry import try_resolve
    ds = try_resolve(ds_id)
    return ds.primary_db_id if ds else ""


def _resolve_db_id(
    *,
    db_id: Optional[str],
    session: Optional[ContextSession],
    driver: Any,
) -> str:
    if db_id:
        return db_id
    derived = _db_id_from_session(session)
    if derived:
        return derived
    try:
        from .retrieval import guess_physical_db_id
        return guess_physical_db_id(driver) or ""
    except Exception:
        return ""


# =========================================================================== #

# Request / Response schemas

class ZoomEntityRequest(BaseModel):
    session_ref: str
    entity_name: str = ""
    entity_key: str = ""


class ExperienceFocus(BaseModel):
    task_type: Optional[str] = None
    avoid_only: bool = False
    with_supersede_chain: bool = False


class RecallExperienceRequest(BaseModel):
    session_ref: str
    focus: Optional[ExperienceFocus] = None


class OutcomeFeedback(BaseModel):
    signal: Literal["success", "fail", "avoid", "supersede", "confirm", "caveat"]
    reason: str = ""
    corrected_sql: str = ""


class RecordOutcomeRequest(BaseModel):
    session_ref: str
    question: str = ""
    sql: str
    exec_status: Literal["success", "error", "empty", "slow", "skipped"]
    feedback: Optional[OutcomeFeedback] = None


class RecordOutcomeResponse(BaseModel):
    status: Literal["queued", "skipped"]
    writeback_kind: str
    trace_id: str
    session_ref: str
    snapshot_id: str
    entry_anchor_keys: list[str]
    cards_applied: list[str]
    is_session_expired: bool
    operation: Optional[dict[str, Any]] = None


def zoom_entity(req: ZoomEntityRequest, request: Request):
    """Three-graph (MG/TG/KG) detail expansion for any entity type."""
    driver = _get_driver(request)
    store: SessionStore = _get_store(request)
    session = _require_session(req.session_ref, store)
    current = session.ensure_current(store)

    entity_key = req.entity_key.strip()
    entity_name = req.entity_name.strip()
    match_confidence = 1.0

    # Resolve entity_key from name if not provided
    if not entity_key and entity_name:
        # Search in anchors first
        for a in getattr(current.anchors, "anchors", []) or []:
            aname = (getattr(a, "name", "") or "").lower()
            if aname == entity_name.lower():
                entity_key = getattr(a, "key", "")
                break
        if not entity_key:
            # Try retrieval search
            from .retrieval import resolve_metric, resolve_column, search_explorer_nodes
            candidates: list[dict[str, Any]] = []
            try:
                candidates = resolve_metric(driver, entity_name, k=3)
            except Exception as exc:
                log.debug("zoom_entity resolve_metric failed: %s", exc)
            if not candidates:
                try:
                    candidates = resolve_column(driver, entity_name, k=3)
                except Exception as exc:
                    log.debug("zoom_entity resolve_column failed: %s", exc)
            if not candidates:
                try:
                    candidates = search_explorer_nodes(driver, entity_name, limit=5)
                except Exception as exc:
                    log.debug("zoom_entity search_explorer_nodes failed: %s", exc)
            if candidates:
                best = candidates[0]
                entity_key = str(best.get("key") or "")
                raw_score = float(best.get("score") or 0.5)
                match_confidence = min(1.0, raw_score * 20)  # RRF scores ~0–0.05 → normalise
    if not entity_key:
        raise HTTPException(status_code=404, detail="Entity not found")

    # Determine label
    label = "Metric"
    for a in getattr(current.anchors, "anchors", []) or []:
        if getattr(a, "key", "") == entity_key:
            label = getattr(a, "label", "Metric")
            break

    # MG: expand_subgraph
    from .retrieval import expand_subgraph as _expand_sg
    try:
        expand_data = _expand_sg(driver, entity_key)
    except Exception:
        expand_data = {"center": None, "nodes": [], "edges": [], "raw": {}}

    # TG: topology neighbors
    topology_neighbors: list[dict[str, Any]] = []
    try:
        from ..utils import neo4j_session
        with neo4j_session(driver) as s:
            topo_cypher = """
            MATCH (n {key: $key})-[r]-(neighbor)
            RETURN neighbor.key AS key, labels(neighbor)[0] AS label,
                   neighbor.name AS name, type(r) AS rel_type
            LIMIT 20
            """
            topo_rows = s.run(topo_cypher, key=entity_key).data()
            for row in topo_rows:
                if row.get("key"):
                    topology_neighbors.append({
                        "key": str(row["key"]),
                        "label": str(row.get("label") or ""),
                        "name": str(row.get("name") or ""),
                        "rel_type": str(row.get("rel_type") or ""),
                    })
    except Exception as exc:
        log.debug("TG neighbor query failed for %s: %s", entity_key, exc)

    # KG: knowledge neighbors
    kg_data: dict[str, Any] = {"neighbors": []}
    try:
        from ..utils import neo4j_session
        with neo4j_session(driver) as s:
            kg_cypher = """
            MATCH (n {key: $key})-[:EXPLAINS|INVOLVES|RELATED_TO]-(kg)
            WHERE kg:Entity OR kg:Event
            RETURN kg.key AS key, labels(kg)[0] AS label, kg.name AS name,
                   kg.description AS description
            LIMIT 10
            """
            kg_rows = s.run(kg_cypher, key=entity_key).data()
            kg_data["neighbors"] = [
                {
                    "key": str(r["key"] or ""),
                    "label": str(r.get("label") or ""),
                    "name": str(r.get("name") or ""),
                    "description": str(r.get("description") or ""),
                }
                for r in kg_rows if r.get("key")
            ]
    except Exception as exc:
        log.debug("KG query failed for %s: %s", entity_key, exc)

    # Sample values for Column/Dimension
    sample_vals: list[str] = []
    if label in ("Column", "Dimension"):
        try:
            from ..utils import neo4j_session
            with neo4j_session(driver) as s:
                val_cypher = """
                MATCH (n {key: $key})
                RETURN n.value_mapping AS vm, n.sample_values AS sv
                LIMIT 1
                """
                vr = s.run(val_cypher, key=entity_key).single()
                if vr:
                    for attr in ("vm", "sv"):
                        raw = vr[attr]
                        if isinstance(raw, (list, tuple)):
                            sample_vals = [str(v) for v in raw[:20]]
                            break
                        elif isinstance(raw, str) and raw:
                            sample_vals = [raw]
                            break
        except Exception:
            pass

    # Related cards: filter cards_visible by entity key / name mention
    related_cards = [
        c for c in (current.cards_visible or [])
        if (
            entity_key in str(c.get("trigger_conditions") or "")
            or entity_name.lower() in str(c.get("strategy_semantics") or "").lower()
        )
    ]

    from .ctx_assemble import _center_props
    from .ctx_trace import build_operation_trace

    center_props = _center_props(expand_data.get("center"))
    entity_name_resolved = entity_name or str(center_props.get("name") or entity_key)

    zoom_steps = [
        {
            "step": 0,
            "id": "resolve_key",
            "title": "解析 entity_key",
            "summary": f"{entity_key} (confidence={match_confidence:.2f})",
            "detail": {"entity_name": entity_name, "label": label},
        },
        {
            "step": 1,
            "id": "mg_expand",
            "title": "MG · expand_subgraph",
            "summary": f"nodes={len(expand_data.get('nodes') or [])} raw_keys={list((expand_data.get('raw') or {}).keys())}",
        },
        {
            "step": 2,
            "id": "tg_neighbors",
            "title": "TG · 拓扑邻居",
            "summary": f"{len(topology_neighbors)} 条边",
        },
        {
            "step": 3,
            "id": "kg_neighbors",
            "title": "KG · 知识邻居",
            "summary": f"{len(kg_data.get('neighbors') or [])} 个 Entity/Event",
        },
        {
            "step": 4,
            "id": "related_cards",
            "title": "关联策略卡",
            "summary": f"{len(related_cards)} 张（trigger 命中）",
        },
    ]
    if sample_vals:
        zoom_steps.append({
            "step": 5,
            "id": "sample_values",
            "title": "采样值",
            "summary": f"{len(sample_vals)} 个",
        })

    operation = build_operation_trace(
        endpoint="zoom_entity",
        pipeline_steps=zoom_steps,
        assembly_steps=[{
            "target": "EntityDetail",
            "sources": ["MG expand + TG/KG 查询 + cards_visible 筛选"],
            "output": "definition / formula / neighbors / related_cards",
        }],
    )

    return assemble_entity_detail_from_expand(
        entity_key=entity_key,
        label=label,
        name=entity_name_resolved,
        match_confidence=match_confidence,
        expand_data=expand_data,
        topology_neighbors=topology_neighbors,
        kg_data=kg_data,
        sample_vals=sample_vals,
        related_cards=related_cards,
        operation=operation,
    )


def recall_experience(req: RecallExperienceRequest, request: Request):
    """Fetch additional experience cards (new snapshot appended)."""
    driver = _get_driver(request)
    store: SessionStore = _get_store(request)
    session = _require_session(req.session_ref, store)
    current = session.ensure_current(store)

    from ..embedder import embed_one
    from ..config import CFG
    from ..graph.strategy_card import StrategyCardRetriever

    focus = req.focus or ExperienceFocus()
    try:
        qemb = embed_one(session.original_query)
    except Exception:
        qemb = []

    card_retriever = StrategyCardRetriever(driver)
    new_recalled = card_retriever.recall_top_k(
        qemb,
        task_type=focus.task_type,
        graph_db_id=(_db_id_from_session(session) or "").strip(),
        k=20,
        allow_avoid=True,
    )

    # Deduplicate against all already-seen card keys in session
    seen_keys = session.all_visible_card_keys()
    new_only = [c for c in new_recalled if str(c.get("key") or "") not in seen_keys]

    if focus.avoid_only:
        new_only = [c for c in new_only if c.get("polarity") in ("negative", "avoid")]

    # Supersede chains if requested
    supersede_chains: list[SupersedeChain] = []
    if focus.with_supersede_chain and new_only:
        try:
            from ..utils import neo4j_session
            with neo4j_session(driver) as s:
                for c in new_only[:5]:
                    ckey = str(c.get("key") or "")
                    if not ckey:
                        continue
                    chain_cypher = """
                    MATCH p = (root {key: $key})-[:SUPERSEDED_BY*1..]->(leaf)
                    RETURN [n IN nodes(p) | n.key] AS chain
                    LIMIT 1
                    """
                    row = s.run(chain_cypher, key=ckey).single()
                    if row and row["chain"]:
                        supersede_chains.append(SupersedeChain(
                            root_key=ckey,
                            chain=[str(k) for k in row["chain"]],
                        ))
        except Exception as exc:
            log.debug("supersede chain query failed: %s", exc)

    # Append new snapshot (reuse anchors/subgraph/decision from parent)
    merged_visible = list(current.cards_visible) + new_only
    snap = make_snapshot(
        trigger="recall_experience",
        query=current.query,
        anchors=current.anchors,
        subgraph=current.subgraph,
        decision=current.decision,
        cards_visible=merged_visible,
        cards_blocked=list(current.cards_blocked),
        top_card_gate=dict(current.top_card_gate),
        facets=list(current.facets),
        parent_id=current.snapshot_id,
        expanded_subgraphs=dict(current.expanded_subgraphs),
        store=store,
    )
    session.append_snapshot(snap)
    store.put(session)

    from .ctx_assemble import _card_brief
    all_scores = [float(c.get("composite_score") or 0.0) for c in merged_visible]
    top_score = max(all_scores) if all_scores else 0.0
    second = sorted(all_scores, reverse=True)[1] if len(all_scores) > 1 else 0.0

    from .ctx_trace import build_operation_trace

    recall_op = build_operation_trace(
        endpoint="recall_experience",
        pipeline_steps=[
            {
                "step": 0,
                "id": "ann_recall",
                "title": "策略卡 ANN (k=20)",
                "summary": f"召回 {len(new_recalled)} → 去重后新增 {len(new_only)}",
                "detail": {
                    "seen_keys_n": len(seen_keys),
                    "focus": focus.model_dump() if hasattr(focus, "model_dump") else {},
                    "anchors_unchanged": True,
                    "subgraph_unchanged": True,
                },
            },
            {
                "step": 1,
                "id": "snapshot_append",
                "title": "追加 Snapshot",
                "summary": f"parent={current.snapshot_id[:12]}… cards_visible={len(merged_visible)}",
            },
        ],
    )

    return ExperienceFragment(
        operation=recall_op,
        new_cards=[_card_brief(c) for c in new_only],
        supersede_chains=supersede_chains,
        stats=ExperienceStats(
            visible_n=len(merged_visible),
            blocked_n=len(current.cards_blocked),
            top_score=top_score,
            gap=top_score - second,
        ),
    )


def record_outcome(req: RecordOutcomeRequest, request: Request):
    """Write SQL execution result + user feedback into the experience graph (async)."""
    driver = _get_driver(request)
    store: SessionStore = _get_store(request)

    trace_id = uuid.uuid4().hex
    is_expired = False

    session = store.get(req.session_ref)
    if session is None:
        is_expired = True
        entry_anchor_keys: list[str] = []
        cards_applied: list[str] = []
        snapshot_id = ""
        # Fallback: use request data to construct minimal writeback
        _dispatch_writeback_fallback(
            driver=driver,
            question=req.question or req.sql[:100],
            sql=req.sql,
            exec_status=req.exec_status,
            feedback=req.feedback,
        )
        writeback_kind = _infer_writeback_kind(req.exec_status, req.feedback)
    else:
        current = session.ensure_current(store)
        snapshot_id = current.snapshot_id
        entry_anchor_keys = [
            getattr(a, "key", "")
            for a in getattr(current.anchors, "anchors", []) or []
        ][:16]
        cards_applied = [
            str(c.get("key") or "")
            for c in (current.cards_visible or [])
            if c.get("key")
        ]
        writeback_kind = _infer_writeback_kind(req.exec_status, req.feedback)

        _dispatch_writeback(
            driver=driver,
            session=session,
            sql=req.sql,
            exec_status=req.exec_status,
            feedback=req.feedback,
        )
        session.append_outcome(OutcomeRecord(
            sql=req.sql,
            exec_status=req.exec_status,
            feedback_signal=req.feedback.signal if req.feedback else None,
            writeback_kind=writeback_kind,
            snapshot_id=snapshot_id,
            trace_id=trace_id,
            queued_at=time.time(),
        ))

    from .ctx_trace import build_operation_trace

    route_reason = []
    if req.feedback:
        route_reason.append(f"feedback.signal={req.feedback.signal}")
    route_reason.append(f"exec_status={req.exec_status}")
    record_op = build_operation_trace(
        endpoint="record_outcome",
        pipeline_steps=[
            {
                "step": 0,
                "id": "route",
                "title": "写回路由",
                "summary": f"→ {writeback_kind}",
                "detail": {"reason": route_reason, "async": True},
            },
        ],
        extras={"is_session_expired": is_expired},
    )

    return RecordOutcomeResponse(
        status="queued",
        writeback_kind=writeback_kind,
        trace_id=trace_id,
        session_ref=req.session_ref,
        snapshot_id=snapshot_id,
        entry_anchor_keys=entry_anchor_keys,
        cards_applied=cards_applied,
        is_session_expired=is_expired,
        operation=record_op,
    )


def _infer_writeback_kind(
    exec_status: str,
    feedback: Optional[OutcomeFeedback],
) -> str:
    if feedback:
        sig = feedback.signal
        if sig in ("avoid", "supersede") and feedback.corrected_sql:
            return "writeback_correction"
        if sig in ("confirm", "caveat"):
            return "writeback_memory_confirm"
        if sig == "fail" and not feedback.corrected_sql:
            return "writeback_user_feedback"
        if sig == "success":
            return "writeback_success"
    if exec_status == "success":
        return "writeback_success"
    if exec_status in ("error", "empty", "slow"):
        return "writeback_failure"
    return "writeback_skipped"


def _dispatch_writeback(
    *,
    driver: Any,
    session: ContextSession,
    sql: str,
    exec_status: str,
    feedback: Optional[OutcomeFeedback],
) -> None:
    """Route to the appropriate writeback_* function (async, daemon thread)."""
    from ..runtime.writeback import record_outcome_dispatch

    current = session.current
    from ..utils import neo4j_database_ctx
    neo4j_db = neo4j_database_ctx.get(None)

    record_outcome_dispatch(
        driver=driver,
        question=session.original_query,
        db_id=_db_id_from_session(session),
        sql=sql,
        exec_status=exec_status,
        decision=current.decision,
        anchors=current.anchors,
        subgraph=current.subgraph,
        feedback_signal=feedback.signal if feedback else None,
        feedback_reason=feedback.reason if feedback else "",
        corrected_sql=feedback.corrected_sql if feedback else "",
        task_key=current.task_key,
        plan_key=current.plan_key,
        neo4j_database=neo4j_db,
    )


def _dispatch_writeback_fallback(
    *,
    driver: Any,
    question: str,
    sql: str,
    exec_status: str,
    feedback: Optional[OutcomeFeedback],
) -> None:
    """Minimal writeback when session has expired."""
    kind = _infer_writeback_kind(exec_status, feedback)
    if kind == "writeback_skipped":
        return
    # With no session context, we can only do minimal writeback
    log.info("record_outcome fallback (expired session): kind=%s", kind)


# =========================================================================== #
