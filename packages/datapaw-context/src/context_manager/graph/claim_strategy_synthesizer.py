"""Claim -> Strategy distillation pipeline (Phase 2-4).

Phase 2: Retrieve related historical tasks by task_signature overlap.
Phase 3: LLM-2 synthesis of Strategy card from claims + trace + history.
Phase 4: Write Strategy node + edges to graph.

Called from ``trace_claim_distiller._async_distill_worker`` after Phase 1 completes.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from neo4j import Driver

from ..config import CFG, llm_thinking_enabled
from ..openai_client import complete_json
from ..utils import neo4j_session

log = logging.getLogger("graph.claim_strategy_synthesizer")


# ---------------------------------------------------------------------- #
# Phase 2: Cypher — retrieve related history
# ---------------------------------------------------------------------- #

_FIND_RELATED_TASKS_CYPHER = """
MATCH (t:Task)
WHERE t.task_signature = $task_signature
  AND t.key <> $exclude_key
  AND t.status IN ['success', 'failed']
RETURN t.key AS task_key, t.status AS status, t.goal AS task_goal
ORDER BY t.created_at DESC
LIMIT $top_k
"""

_FETCH_HISTORY_CLAIMS_CYPHER = """
MATCH (t:Task {key: $task_key})
OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(p:Step)-[:EXECUTED_BY]->(tc:ToolCall)
  -[:PRODUCES]->(cl:Claim)
RETURN cl.text AS claim_text, cl.confidence AS claim_confidence
"""

_FETCH_HISTORY_TRACE_CYPHER = """
MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)
WITH p ORDER BY coalesce(p.step_idx, 0)
OPTIONAL MATCH (p)-[:EXECUTED_BY]->(tc:ToolCall)
WITH p, tc ORDER BY coalesce(p.step_idx, 0), coalesce(tc.ts, datetime('1970-01-01'))
RETURN p.step_idx AS step_idx, p.intent AS intent,
       coalesce(tc.observation_summary, '') AS obs_summary
"""


def _build_trace_excerpt(rows: list[dict], max_chars: int) -> str:
    """Build compact trace text from history rows (step_idx, intent, obs_summary)."""
    if not rows:
        return ""
    lines: list[str] = []
    total = 0
    for r in rows:
        idx = r.get("step_idx") or 0
        intent = r.get("intent") or ""
        obs = r.get("obs_summary") or ""
        line = f"  Step {idx}: {intent}"
        if obs:
            short_obs = obs[:120]
            line += f" -> {short_obs}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def retrieve_related_history(
    driver: Driver,
    task_signature: str,
    *,
    exclude_task_key: str = "",
    top_k: int = 5,
) -> list[dict]:
    """Find historical tasks with the same task_signature, enrich with claims and trace excerpts.

    Args:
        driver: Neo4j driver.
        task_signature: The current task's signature hash.
        exclude_task_key: Current task key to exclude from results.
        top_k: Maximum number of related tasks to return.

    Returns:
        List of dicts with keys: task_key, status, task_goal,
        claims (list of {claim_text, claim_confidence}), trace_excerpt (str).
    """
    if not task_signature:
        return []

    max_excerpt = CFG.claim_strategy_trace_excerpt_chars

    with neo4j_session(driver) as s:
        task_rows = s.run(
            _FIND_RELATED_TASKS_CYPHER,
            task_signature=task_signature,
            exclude_key=exclude_task_key,
            top_k=top_k,
        ).data()

    if not task_rows:
        return []

    results: list[dict] = []
    for row in task_rows:
        tk = row.get("task_key") or ""
        if not tk:
            continue

        # Fetch claims for this history task
        with neo4j_session(driver) as s:
            claim_rows = s.run(
                _FETCH_HISTORY_CLAIMS_CYPHER, task_key=tk,
            ).data()

        claims = []
        for cr in (claim_rows or []):
            ct = cr.get("claim_text") or ""
            if ct:
                claims.append({
                    "claim_text": ct,
                    "claim_confidence": cr.get("claim_confidence"),
                })

        # Fetch trace excerpt for this history task
        with neo4j_session(driver) as s:
            trace_rows = s.run(
                _FETCH_HISTORY_TRACE_CYPHER, task_key=tk,
            ).data()

        excerpt = _build_trace_excerpt(trace_rows or [], max_excerpt)

        results.append({
            "task_key": tk,
            "status": row.get("status") or "",
            "task_goal": row.get("task_goal") or "",
            "claims": claims,
            "trace_excerpt": excerpt,
        })

    return results


# ---------------------------------------------------------------------- #
# Phase 3: LLM-2 strategy synthesis
# ---------------------------------------------------------------------- #

_STRATEGY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "should_skip": {
            "type": "boolean",
            "description": "Set true when the task is too trivial or routine to yield reusable strategy.",
        },
        "strategy_semantics": {"type": "string"},
        "anchored_nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Graph node key (e.g., met:ChatApp:DAU)"},
                    "polarity": {"type": "string", "enum": ["positive", "negative"]},
                    "reason": {"type": "string", "description": "Why this node is anchored (positive/negative)"},
                },
                "required": ["key", "polarity"],
            },
        },
    },
    "required": ["should_skip"],
}


_APPLY_SYSTEM_PROMPT = (
    "You are an experience-distillation agent. A data-analysis task just "
    "completed successfully. Decide whether to summarise the run as a reusable "
    "**apply** strategy card, or skip it.\n\n"
    "## When to skip (should_skip=true)\n"
    "- The task was trivial / routine (e.g., a simple single-table lookup).\n"
    "- No non-obvious insight, decision, or pitfall was encountered.\n"
    "- The claims themselves are mostly trivial mappings or schema descriptions.\n"
    "When skipping, set should_skip=true and leave strategy_semantics empty.\n\n"
    "## When to generate (should_skip=false)\n"
    "Write a 90-220 word English paragraph.\n"
    "- Lead with the information-need type (metric lookup / join resolution / "
    "caliber clarification / time-window handling).\n"
    "- Centre the experience: what insight or constraint made this approach work, "
    "what trade-off was made.\n"
    "- Do NOT list graph keys, SQL text, or implementation details.\n"
    "- Output JSON with fields: should_skip (bool), strategy_semantics (string), "
    "anchored_nodes (array of {key, polarity, reason}).\n"
    "\n"
    "**Anchored nodes**: Identify graph nodes (Metric/Table/Column/Event) "
    "that the trace accessed. Mark them as:\n"
    "- `positive`: Successfully used in the final answer path (e.g., the correct "
    "DAU metric, the right table for retention).\n"
    "- `negative`: Attempted but abandoned or caused errors (e.g., wrong table "
    "that led to Cartesian product, incorrect metric definition).\n"
    "- Provide the node key (e.g., met:ChatApp:DAU, tbl:public.wrong_table) "
    "and a brief reason."
)

_AVOID_SYSTEM_PROMPT = (
    "You are an experience-distillation agent. A data-analysis task just "
    "failed. Decide whether to summarise what to **avoid** in similar future "
    "runs, or skip it.\n\n"
    "## When to skip (should_skip=true)\n"
    "- The failure was due to an external/transient cause (network timeout, "
    "auth error, user cancellation) with no reusable lesson.\n"
    "- The failure mode is already well-covered by existing strategy cards or "
    "claims visible in the prompt.\n"
    "When skipping, set should_skip=true and leave strategy_semantics empty.\n\n"
    "## When to generate (should_skip=false)\n"
    "Write a 90-220 word English paragraph.\n"
    "- Lead with the failure scenario (what went wrong).\n"
    "- Explain the root cause (wrong table, missing predicate, illegal join).\n"
    "- Suggest an alternative approach.\n"
    "- Output JSON with fields: should_skip (bool), strategy_semantics (string), "
    "anchored_nodes (array of {key, polarity, reason}).\n"
    "\n"
    "**Anchored nodes**: Identify graph nodes (Metric/Table/Column/Event) "
    "that the trace accessed. Mark them as:\n"
    "- `positive`: Nodes that are still valid/reusable despite the failure "
    "(e.g., correct metric definition even if the query failed for other reasons).\n"
    "- `negative`: Nodes that contributed to the failure (e.g., wrong table, "
    "missing predicate on a specific column).\n"
    "- Provide the node key and a brief reason."
)


def _fallback_strategy(
    status: str,
    claims: list[dict],
) -> str:
    """Deterministic fallback when LLM fails or returns too-short text."""
    claim_strs = [c.get("text", "")[:80] for c in claims[:3] if c.get("text")]
    claim_join = "; ".join(claim_strs)
    polarity = "successful" if status == "success" else "failed"
    return (
        f"Experience ({polarity} run). Key claims: {claim_join}. "
        f"Validated pattern for this task class."
    )[:1200]


def _build_synthesis_prompt(
    trace: dict,
    claims: list[dict],
    history: list[dict],
) -> str:
    """Build user prompt including current task info + claims + history."""
    lines: list[str] = []

    # Current task
    goal = trace.get("goal") or ""
    status = trace.get("status") or "unknown"
    failure_lesson = trace.get("failure_lesson") or ""
    lines.append(f"Task goal: {goal}")
    lines.append(f"Status: {status}")
    if failure_lesson:
        lines.append(f"Failure lesson: {failure_lesson}")
    lines.append("")

    # Claims
    if claims:
        lines.append("Claims:")
        for i, c in enumerate(claims, 1):
            text = c.get("text", "")
            conf = c.get("confidence")
            conf_str = f" (conf={conf:.2f})" if conf is not None else ""
            lines.append(f"  {i}. {text}{conf_str}")
        lines.append("")

    # Trace summary
    steps = trace.get("steps") or []
    if steps:
        lines.append("Trace summary:")
        for step in steps:
            idx = step.get("step_idx", 0)
            intent = step.get("intent") or ""
            lines.append(f"  Step {idx}: {intent}")
            for tc in step.get("tool_calls") or []:
                obs = tc.get("obs_summary") or ""
                if obs:
                    short = obs[:200]
                    lines.append(f"    -> {short}")
        lines.append("")

    # Historical experiences
    if history:
        lines.append("Related historical experiences:")
        for h in history:
            h_goal = h.get("task_goal") or ""
            h_status = h.get("status") or ""
            lines.append(f"  [{h_status}] {h_goal}")
            h_claims = h.get("claims") or []
            for hc in h_claims[:3]:
                ct = hc.get("claim_text") or ""
                if ct:
                    lines.append(f"    claim: {ct[:120]}")
            excerpt = h.get("trace_excerpt") or ""
            if excerpt:
                lines.append(f"    trace: {excerpt[:300]}")
        lines.append("")

    return "\n".join(lines)


def synthesize_strategy(
    driver: Driver,
    task_key: str,
    trace: dict,
    claims: list[dict],
    history: list[dict],
    *,
    model: Optional[str] = None,
) -> Optional[dict]:
    """LLM-2 synthesis: produce strategy_semantics + polarity + anchored_nodes.

    The LLM may decide to skip strategy generation (``should_skip=true``) when
    the task is too trivial or the failure has no reusable lesson. In that case
    this function returns ``None`` and no Strategy card is written.

    Args:
        driver: Neo4j driver (unused directly but kept for API consistency).
        task_key: Current task key.
        trace: Trace dict with goal, status, failure_lesson, steps.
        claims: List of claim dicts from Phase 1.
        history: Output of retrieve_related_history.
        model: Optional model override.

    Returns:
        Dict with keys: strategy_semantics, polarity, anchored_nodes.
        Returns None when the LLM decides to skip (should_skip=true) or if
        everything fails.
    """
    status = trace.get("status") or "unknown"
    polarity = "apply" if status == "success" else "avoid"
    system_prompt = _APPLY_SYSTEM_PROMPT if polarity == "apply" else _AVOID_SYSTEM_PROMPT

    user_prompt = _build_synthesis_prompt(trace, claims, history)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    sem_think = llm_thinking_enabled("strategy_semantics_distill", context="agent")

    result_text = ""
    anchored_nodes: list[dict] = []
    should_skip = False
    try:
        parsed = complete_json(
            messages,
            json_schema=_STRATEGY_JSON_SCHEMA,
            model=model,
            max_retries=2,
            temperature=CFG.claim_strategy_temperature,
            enable_thinking=sem_think,
        )
        if isinstance(parsed, dict):
            should_skip = bool(parsed.get("should_skip"))
            if should_skip:
                log.info(
                    "synthesize_strategy: LLM skipped strategy for %s (trivial/not-reusable)",
                    task_key,
                )
                return None
            result_text = str(parsed.get("strategy_semantics") or "").strip()
            raw_an = parsed.get("anchored_nodes") or []
            if isinstance(raw_an, list):
                for item in raw_an:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "").strip()
                    pol = str(item.get("polarity") or "positive").strip().lower()
                    if not key or pol not in ("positive", "negative"):
                        continue
                    reason = str(item.get("reason") or "").strip()[:200]
                    anchored_nodes.append({"key": key, "polarity": pol, "reason": reason})
    except Exception as exc:
        log.warning("synthesize_strategy: LLM call failed: %s", exc)

    # Fallback if LLM failed or returned too-short text
    if len(result_text) < 40:
        result_text = _fallback_strategy(status, claims)

    return {
        "strategy_semantics": result_text,
        "polarity": polarity,
        "anchored_nodes": anchored_nodes,
    }


# ---------------------------------------------------------------------- #
# Phase 4: Write Strategy card to graph
# ---------------------------------------------------------------------- #

def write_strategy_card(
    driver: Driver,
    task_key: str,
    card: dict,
    claim_keys: list[str],
) -> str:
    """Write Strategy node + edges to graph.

    Steps:
      1. Generate card key.
      2. Embed semantics.
      2b. Calculate initial confidence from anchor count, avg claim confidence,
          and task status (base 0.3, +0.1 per anchor capped at +0.3, +0.1 if
          avg claim conf > 0.8, +0.1 if task status == success, cap 0.9).
      3. MERGE Strategy node (writes ``st.confidence``).
      4. DERIVED_FROM -> Task.
      5. DISTILLED_FROM -> each Claim (optional match).
      6. ANCHORED_TO -> Table/Column/Metric/Entity nodes reached via
         Claim.RESOLVED_TO and ToolCall.EVIDENCED_BY.

    Args:
        driver: Neo4j driver.
        task_key: Source Task key.
        card: Dict with strategy_semantics, polarity, anchored_nodes.
        claim_keys: Claim node keys to link via DISTILLED_FROM.

    Returns:
        The generated card key string.
    """
    from .keys import card_key as _card_key_fn

    semantics = card.get("strategy_semantics") or ""
    polarity = card.get("polarity") or "apply"

    # 1) Generate card key
    c_key = _card_key_fn(
        task_signature=semantics[:256],
        anchor_label_types=[polarity],
        question_summary=semantics[:120],
        variant_suffix=polarity,
    )

    # 2) Embed semantics
    embedding: Optional[list[float]] = None
    try:
        from ..embedder import embed_one
        embedding = embed_one(semantics)
    except Exception as exc:
        log.warning("write_strategy_card: embed failed for %s: %s", c_key, exc)

    # 2b) Calculate initial confidence from anchors, claim confidence, status
    anchor_count = 0
    avg_conf: float = 0.5
    task_status = ""
    try:
        with neo4j_session(driver) as s:
            # Count anchors (distinct graph nodes reached via Claim.RESOLVED_TO)
            anchor_count = s.run(
                """
                MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)-[:EXECUTED_BY]->(tc:ToolCall)
                  -[:PRODUCES]->(cl:Claim)-[:RESOLVED_TO]->(n)
                RETURN count(DISTINCT n) AS cnt
                """,
                task_key=task_key,
            ).data()[0]["cnt"]

            # Average claim confidence
            avg_conf_row = s.run(
                """
                MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)-[:EXECUTED_BY]->(tc:ToolCall)
                  -[:PRODUCES]->(cl:Claim)
                RETURN avg(cl.confidence) AS avg_conf
                """,
                task_key=task_key,
            ).data()
            avg_conf = (avg_conf_row[0].get("avg_conf") if avg_conf_row else None) or 0.5

            # Task status
            status_row = s.run(
                "MATCH (t:Task {key: $task_key}) RETURN t.status AS status",
                task_key=task_key,
            ).data()
            task_status = (status_row[0].get("status") if status_row else None) or ""
    except Exception as exc:
        log.warning("write_strategy_card: confidence calc failed for %s: %s", c_key, exc)

    # Confidence formula: base 0.3 + anchors (capped) + high avg conf + success
    confidence = 0.3
    confidence += min(0.3, anchor_count * 0.1)  # up to +0.3 for anchors
    if avg_conf > 0.8:
        confidence += 0.1
    if task_status == "success":
        confidence += 0.1
    confidence = min(0.9, confidence)  # cap at 0.9

    with neo4j_session(driver) as s:
        # 3) MERGE Strategy node
        s.run(
            """
            MERGE (st:Strategy {key: $key})
              ON CREATE SET
                st.strategy_semantics = $semantics,
                st.polarity = $polarity,
                st.source = 'claim_distill',
                st.claim_keys = $claim_keys,
                st.signature_emb = $embedding,
                st.confidence = $confidence,
                st.zone = 'trace',
                st.created_at = datetime(),
                st.ingest_at = datetime()
              ON MATCH SET
                st.strategy_semantics = $semantics,
                st.polarity = $polarity,
                st.signature_emb = $embedding,
                st.confidence = $confidence
            """,
            key=c_key,
            semantics=semantics,
            polarity=polarity,
            claim_keys=claim_keys,
            embedding=embedding,
            confidence=confidence,
        )

        # 4) DERIVED_FROM -> Task
        s.run(
            """
            MATCH (st:Strategy {key: $card_key})
            MATCH (t:Task {key: $task_key})
            MERGE (st)-[r:DERIVED_FROM]->(t)
              ON CREATE SET r.source = 'claim_distill', r.created_at = datetime()
            """,
            card_key=c_key,
            task_key=task_key,
        )

        # 5) DISTILLED_FROM -> each Claim (OPTIONAL MATCH)
        for ck in claim_keys:
            if not ck:
                continue
            s.run(
                """
                MATCH (st:Strategy {key: $card_key})
                OPTIONAL MATCH (cl:Claim {key: $claim_key})
                FOREACH (_ IN CASE WHEN cl IS NULL THEN [] ELSE [cl] END |
                    MERGE (st)-[r:DISTILLED_FROM]->(cl)
                      ON CREATE SET r.source = 'claim_distill', r.created_at = datetime()
                )
                """,
                card_key=c_key,
                claim_key=ck,
            )

        # 6) ANCHORED_TO -> graph nodes with LLM-judged polarity
        # Use LLM-provided anchored_nodes if available, otherwise fall back to
        # automatic discovery via Claim.RESOLVED_TO and ToolCall.EVIDENCED_BY
        anchored_nodes = card.get("anchored_nodes") or []
        if anchored_nodes:
            # Use LLM-provided anchors with polarity
            for anchor in anchored_nodes:
                node_key = anchor.get("key") or ""
                anchor_polarity = anchor.get("polarity") or "positive"
                reason = anchor.get("reason") or ""
                if not node_key:
                    continue
                s.run(
                    """
                    MATCH (st:Strategy {key: $card_key})
                    OPTIONAL MATCH (n {key: $node_key})
                    FOREACH (_ IN CASE WHEN n IS NULL THEN [] ELSE [n] END |
                        MERGE (st)-[r:ANCHORED_TO]->(n)
                          ON CREATE SET
                            r.source = 'claim_distill',
                            r.polarity = $polarity,
                            r.reason = $reason,
                            r.created_at = datetime()
                    )
                    """,
                    card_key=c_key,
                    node_key=node_key,
                    polarity=anchor_polarity,
                    reason=reason,
                )
        else:
            # Fallback: automatic discovery (no polarity info)
            anchored = s.run(
                """
                MATCH (st:Strategy {key: $card_key})-[:DISTILLED_FROM]->(cl:Claim)-[:RESOLVED_TO]->(n)
                RETURN DISTINCT n.key AS node_key, labels(n) AS labels
                UNION
                MATCH (st:Strategy {key: $card_key})-[:DERIVED_FROM]->(t:Task)
                  -[:DECOMPOSES_INTO]->(p:Step)-[:EXECUTED_BY]->(tc:ToolCall)
                  -[:EVIDENCED_BY]->(n)
                RETURN DISTINCT n.key AS node_key, labels(n) AS labels
                """,
                card_key=c_key,
            ).data()

            for row in anchored:
                node_key = row.get("node_key")
                if not node_key:
                    continue
                s.run(
                    """
                    MATCH (st:Strategy {key: $card_key})
                    MATCH (n {key: $node_key})
                    MERGE (st)-[r:ANCHORED_TO]->(n)
                      ON CREATE SET r.source = 'claim_distill', r.polarity = 'positive', r.created_at = datetime()
                    """,
                    card_key=c_key,
                    node_key=node_key,
                )

    return c_key


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #

_COLLECT_CLAIM_KEYS_CYPHER = """
MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)
  -[:EXECUTED_BY]->(tc:ToolCall)-[:PRODUCES]->(cl:Claim)
RETURN DISTINCT cl.key AS claim_key
"""

_FETCH_TASK_SIGNATURE_CYPHER = """
MATCH (t:Task {key: $task_key})
RETURN t.task_signature AS task_signature
"""


def distill_strategy_from_trace(
    driver: Driver,
    task_key: str,
    trace: dict,
    claims: list[dict],
) -> Optional[str]:
    """Orchestrate Phase 2-4: history retrieval -> synthesis -> write card.

    Args:
        driver: Neo4j driver.
        task_key: Current task key.
        trace: Trace dict from Phase 1.
        claims: Claims list from Phase 1.

    Returns:
        The generated Strategy card key, or None if skipped.
    """
    if not claims:
        log.info(
            "distill_strategy_from_trace: no claims for %s, skipping",
            task_key,
        )
        return None

    # Fetch task_signature for history retrieval
    task_signature = ""
    try:
        with neo4j_session(driver) as s:
            row = s.run(
                _FETCH_TASK_SIGNATURE_CYPHER, task_key=task_key,
            ).single()
            if row:
                task_signature = str(row.get("task_signature") or "")
    except Exception as exc:
        log.warning(
            "distill_strategy_from_trace: failed to fetch task_signature for %s: %s",
            task_key, exc,
        )

    # Phase 2: retrieve related history
    history = retrieve_related_history(
        driver,
        task_signature,
        exclude_task_key=task_key,
        top_k=CFG.claim_strategy_history_top_k,
    )

    # Phase 3: synthesize strategy
    card = synthesize_strategy(
        driver, task_key, trace, claims, history,
    )
    if card is None:
        log.info(
            "distill_strategy_from_trace: LLM skipped strategy for %s",
            task_key,
        )
        return None

    # Collect claim keys from graph
    claim_keys: list[str] = []
    try:
        with neo4j_session(driver) as s:
            rows = s.run(
                _COLLECT_CLAIM_KEYS_CYPHER, task_key=task_key,
            ).data()
            claim_keys = [
                r.get("claim_key") or ""
                for r in (rows or [])
                if r.get("claim_key")
            ]
    except Exception as exc:
        log.warning(
            "distill_strategy_from_trace: failed to collect claim keys for %s: %s",
            task_key, exc,
        )

    # Phase 4: write strategy card
    try:
        c_key = write_strategy_card(driver, task_key, card, claim_keys)
    except Exception as exc:
        log.warning(
            "distill_strategy_from_trace: write_strategy_card failed for %s: %s",
            task_key, exc,
        )
        return None

    log.info(
        "distill_strategy_from_trace: task=%s card=%s polarity=%s history=%d claims=%d",
        task_key, c_key, card.get("polarity"),
        len(history), len(claim_keys),
    )
    return c_key


__all__ = [
    "distill_strategy_from_trace",
    "update_strategy_confidence_from_execution",
]


# ---------------------------------------------------------------------- #
# Layer 2: Execution path feedback
# ---------------------------------------------------------------------- #

def update_strategy_confidence_from_execution(
    driver: Driver,
    strategy_key: str,
    task_key: str,
    execution_success: bool,
    *,
    anchor_overlap_threshold: float = 0.3,
) -> Optional[float]:
    """根据执行路径反馈更新 Strategy 卡的置信度。

    算法:
      1. 检查是否是"同任务": 图节点重叠（通过 anchored_nodes vs task claims）
      2. 判断"真成功": execution_success + 无负面反馈
      3. 更新 confidence:
         - 同任务 + 真成功: confidence += 0.05 (cap 0.95)
         - 同任务 + 真失败: confidence -= 0.10 (floor 0.1)
         - 不同任务: 不更新

    Args:
        driver: Neo4j driver.
        strategy_key: Strategy 卡 key。
        task_key: 当前任务 key。
        execution_success: 任务是否成功执行。
        anchor_overlap_threshold: 图节点重叠阈值。

    Returns:
        更新后的 confidence，或 None（未更新）。
    """
    with neo4j_session(driver) as s:
        # 1. 获取 Strategy 卡的 anchored_nodes
        strategy_data = s.run(
            """
            MATCH (st:Strategy {key: $strategy_key})
            OPTIONAL MATCH (st)-[:ANCHORED_TO]->(anchor)
            RETURN st.confidence AS current_conf,
                   collect(DISTINCT anchor.key) AS anchored_keys
            """,
            strategy_key=strategy_key,
        ).data()

        if not strategy_data or not strategy_data[0].get("current_conf"):
            log.warning(
                "update_strategy_confidence_from_execution: strategy %s not found",
                strategy_key,
            )
            return None

        current_conf = strategy_data[0]["current_conf"]
        anchored_keys = strategy_data[0].get("anchored_keys", [])

        # 2. 获取当前任务访问的图节点（通过 Claim.RESOLVED_TO 和 ToolCall.EVIDENCED_BY）
        task_anchors = s.run(
            """
            MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)-[:EXECUTED_BY]->(tc:ToolCall)
            OPTIONAL MATCH (tc)-[:PRODUCES]->(cl:Claim)-[:RESOLVED_TO]->(n1)
            OPTIONAL MATCH (tc)-[:EVIDENCED_BY]->(n2)
            RETURN collect(DISTINCT coalesce(n1.key, n2.key)) AS anchor_keys
            """,
            task_key=task_key,
        ).data()
        task_anchor_list = task_anchors[0].get("anchor_keys", []) if task_anchors else []

        # 3. 计算重叠度
        anchor_overlap = (
            len(set(anchored_keys) & set(task_anchor_list)) / len(anchored_keys)
            if anchored_keys else 0
        )

        is_same_task = anchor_overlap >= anchor_overlap_threshold

        if not is_same_task:
            log.info(
                "update_strategy_confidence_from_execution: task %s not same type as strategy %s (anchor_overlap=%.2f)",
                task_key, strategy_key, anchor_overlap,
            )
            return None

        # 4. 更新 confidence
        if execution_success:
            new_conf = min(0.95, current_conf + 0.05)
            delta = "+0.05"
        else:
            new_conf = max(0.1, current_conf - 0.10)
            delta = "-0.10"

        s.run(
            """
            MATCH (st:Strategy {key: $strategy_key})
            SET st.confidence = $new_conf,
                st.last_feedback_at = datetime()
            """,
            strategy_key=strategy_key,
            new_conf=new_conf,
        )

        log.info(
            "update_strategy_confidence_from_execution: strategy %s confidence %.2f → %.2f (%s)",
            strategy_key, current_conf, new_conf, delta,
        )
        return new_conf
