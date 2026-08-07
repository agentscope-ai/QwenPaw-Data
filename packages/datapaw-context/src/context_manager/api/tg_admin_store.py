"""TG management write layer — Task status, Claim editing, Strategy Card editing.

Thin wrappers that enforce whitelist validation then delegate to Neo4j.
"""
from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks
from neo4j import Driver

from ..utils import get_logger, graph_session

log = get_logger("api.tg_admin_store")

# ═══════════════════════════════════════════════════════════════════════ #
#  Task list (independent Cypher, not reusing trace_api.list_traces)
# ═══════════════════════════════════════════════════════════════════════ #

def list_tasks(
    driver: Driver,
    *,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """Paginated Task listing with step/claim counts per task."""
    conditions = ["t.zone = 'trace'"]
    params: dict[str, Any] = {}
    if status:
        conditions.append("t.status = $status")
        params["status"] = status
    if date_from:
        conditions.append("toString(t.created_at) >= $date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("toString(t.created_at) <= $date_to")
        params["date_to"] = date_to
    if q:
        conditions.append("toLower(coalesce(t.goal, '')) CONTAINS $q")
        params["q"] = q.lower()
    where = " AND ".join(conditions)
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size

    count_cypher = f"MATCH (t:Task) WHERE {where} RETURN count(t) AS total"
    data_cypher = f"""
    MATCH (t:Task)
    WHERE {where}
    OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(p:Step)
    OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(:Step)-[:EXECUTED_BY]->(:ToolCall)
                   -[:PRODUCES]->(c:Claim)
    WITH t, count(DISTINCT p) AS step_count, count(DISTINCT c) AS claim_count
    RETURN t.key AS key,
           coalesce(t.goal, '') AS goal,
           coalesce(t.status, '') AS status,
           coalesce(t.task_signature, '') AS task_signature,
           toString(t.created_at) AS created_at,
           step_count,
           claim_count
    ORDER BY t.created_at DESC
    SKIP $skip LIMIT $limit
    """
    with graph_session(driver) as s:
        total_rec = s.run(count_cypher, **{k: v for k, v in params.items() if k not in ("skip", "limit")}).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()
    return rows, total


# ═══════════════════════════════════════════════════════════════════════ #
#  Task status change
# ═══════════════════════════════════════════════════════════════════════ #

_VALID_STATUS_TRANSITIONS = {
    ("success", "archived"),
    ("failed", "archived"),
    ("success", "invalidated"),
    ("failed", "invalidated"),
    ("running", "invalidated"),
    ("archived", "invalidated"),
}


def update_task_status(
    driver: Driver, task_key: str, *, new_status: str, reason: str | None = None,
) -> dict:
    """Transition a task's status with state-machine validation."""
    with graph_session(driver) as s:
        rec = s.run(
            "MATCH (t:Task {key: $key}) RETURN t.status AS status",
            key=task_key,
        ).single()
    if not rec:
        raise ValueError(f"Task not found: {task_key}")
    current = rec["status"] or ""
    if (current, new_status) not in _VALID_STATUS_TRANSITIONS:
        raise ValueError(
            f"不允许状态转换: {current} → {new_status}"
        )
    with graph_session(driver) as s:
        s.run(
            """
            MATCH (t:Task {key: $key})
            SET t.status = $new_status,
                t.status_reason = $reason,
                t.status_updated_at = datetime()
            """,
            key=task_key, new_status=new_status, reason=reason or "",
        )
    return {"ok": True, "key": task_key, "status": new_status}


# ═══════════════════════════════════════════════════════════════════════ #
#  Claim field editing
# ═══════════════════════════════════════════════════════════════════════ #

_CLAIM_EDITABLE_FIELDS = frozenset({"text", "confidence", "subject_type", "predicate", "object"})


def update_claim_fields(
    driver: Driver,
    claim_key: str,
    *,
    updates: dict[str, Any],
    background_tasks: BackgroundTasks | None = None,
) -> dict:
    """Partially update whitelisted Claim fields; recomputes embedding if text changes."""
    bad_keys = set(updates.keys()) - _CLAIM_EDITABLE_FIELDS
    if bad_keys:
        raise ValueError(f"不允许编辑字段: {', '.join(sorted(bad_keys))}")
    if not updates:
        raise ValueError("updates 不能为空")
    with graph_session(driver) as s:
        rec = s.run(
            """
            MATCH (c:Claim {key: $key})
            SET c += $updates, c.updated_at = datetime()
            RETURN c.key AS key
            """,
            key=claim_key, updates=updates,
        ).single()
    if not rec:
        raise ValueError(f"Claim not found: {claim_key}")
    if "text" in updates and background_tasks is not None:
        background_tasks.add_task(_recompute_claim_embedding, driver, claim_key, updates["text"])
    return {"ok": True, "key": rec["key"]}


def _recompute_claim_embedding(driver: Driver, claim_key: str, new_text: str) -> None:
    """Background task: re-embed a Claim after its text is edited."""
    try:
        from ..graph.trace import _safe_embed
        vec, hash_val = _safe_embed(new_text)
        if vec is not None:
            with graph_session(driver) as s:
                s.run(
                    """
                    MATCH (c:Claim {key: $key})
                    SET c.embedding = $vec, c.embedding_hash = $hash
                    """,
                    key=claim_key, vec=vec, hash=hash_val,
                )
            log.info("Claim %s embedding recomputed", claim_key)
    except Exception:
        log.warning("Failed to recompute embedding for Claim %s", claim_key, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════ #
#  Strategy Card field editing
# ═══════════════════════════════════════════════════════════════════════ #

_STRATEGY_EDITABLE_FIELDS = frozenset({
    "strategy_semantics", "memory_tier", "source_trust", "polarity", "example_query",
})


def update_strategy_card_fields(
    driver: Driver, card_key: str, *, updates: dict[str, Any],
) -> dict:
    """Partially update whitelisted Strategy Card fields."""
    bad_keys = set(updates.keys()) - _STRATEGY_EDITABLE_FIELDS
    if bad_keys:
        raise ValueError(f"不允许编辑字段: {', '.join(sorted(bad_keys))}")
    if not updates:
        raise ValueError("updates 不能为空")
    with graph_session(driver) as s:
        rec = s.run(
            """
            MATCH (s:Strategy {key: $key})
            SET s += $updates, s.updated_at = datetime()
            RETURN s.key AS key
            """,
            key=card_key, updates=updates,
        ).single()
    if not rec:
        raise ValueError(f"Strategy card not found: {card_key}")
    return {"ok": True, "key": rec["key"]}
