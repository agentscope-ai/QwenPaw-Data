"""Distill **experience** text for Neo4j ``:Strategy`` nodes + signature_emb — lessons and rationale, not path dumps.

The paragraph must read like a reusable memo: intent, why this approach worked, caveats. Light graph/SQL
hints are optional; long lists of node keys are avoided. Used for ANN (embedding) and UI.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..config import CFG
from ..openai_client import complete_json, resolve_llm_model

log = logging.getLogger("runtime.strategy_semantics")

STRATEGY_SEMANTICS_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["strategy_semantics"],
    "properties": {
        "strategy_semantics": {"type": "string"},
    },
}

_APPLY_SYSTEM = """You write **query experience** memos for a Strategy node (procedural memory): what was learned in this successful run,
not a catalogue of graph keys or join steps.

Inputs may include: user_question, task_type, decision_rationale (from the planner — treat as the primary
story of *why* this approach was chosen), negative_hints, optional path_plan / path_subgraph_keys /
sql_template as supporting detail only.

Hard rules:
1. Target about 90–220 English words; plain prose; no Markdown, no JSON, no bullet lists of raw keys.
2. Lead with the **information need archetype** (paraphrase the question class so similar questions embed nearby).
3. **Center the experience**: causal reasoning — what insight or constraint made this answer correct (grain,
   time window, filters, metric definition, join semantics). Prefer **decision_rationale** when present;
   synthesize it into your own words; do not paste it verbatim if it is already long.
4. Mention graph/SQL shape only as short intuition (e.g. “aggregate at daily grain before ranking”), not
   as enumeration of path_subgraph_keys. At most one short clause naming 1–2 semantic roles if it helps
   disambiguation (e.g. revenue metric vs order fact).
5. If user_feedback_or_correction is present, state how the correction changes the reusable lesson.
6. Close with one sentence that this run validated the pattern (usable for future similar questions).

Do **not** fill the text with colon-separated key lists or copy more than a handful of opaque identifiers."""

_AVOID_SYSTEM = """You write **failure experience** memos: what went wrong and what a future run should learn,
not a dump of failed node keys.

Hard rules:
1. Target about 90–220 English words; one paragraph; no Markdown, no JSON; no long key lists.
2. Open with the **scenario or question shape** that fails with the attempted approach.
3. Explain the **lesson**: wrong grain, missing predicate, illegal join, wrong metric, env error, etc.
   Use error_or_lesson as the factual anchor.
4. Say what kind of **different strategy** would be needed (high level), without prescribing full SQL.
5. End clearly that this path pattern is not reusable for this intent."""


def _fallback_apply(
    question: str,
    task_type: str,
    path_subgraph_keys: list[str],
    path_plan: Optional[list[str]],
    sql_template: str,
    card_reason: str = "",
    negative_hints: Optional[list[str]] = None,
) -> str:
    cr = (card_reason or "").strip()
    nh = [str(h).strip() for h in (negative_hints or []) if str(h).strip()][:5]
    pp = ""
    if path_plan:
        pp = "Ontology hops that worked: " + "; ".join(path_plan[:6]) + ". "
    tail = (sql_template or "").strip().replace("\n", " ")
    if len(tail) > 240:
        tail = tail[:240] + "…"
    caveats = ""
    if nh:
        caveats = " Caveats to carry forward: " + "; ".join(nh) + "."
    if cr:
        return (
            f"[{task_type}] Experience: {cr[:520]}"
            f"{caveats} Question class: «{question[:140]}». {pp}"
            f"Validated SQL pattern (abbrev.): {tail}"
        )[:1200]
    sample_keys = ", ".join(path_subgraph_keys[:4])
    extra = f" Representative graph anchors: {sample_keys}." if sample_keys else ""
    return (
        f"[{task_type}] Experience: successful run for «{question[:160]}». {pp}{extra}"
        f"{caveats} SQL pattern (abbrev.): {tail}"
    )[:1200]


def _fallback_avoid(
    question: str,
    task_type: str,
    path_subgraph_keys: list[str],
    lesson: str,
) -> str:
    lesson_t = (lesson or "").strip()[:400]
    sample = ", ".join(path_subgraph_keys[:3])
    tail = f" (involved: {sample})" if sample else ""
    return (
        f"[{task_type}] Failed experience: for «{question[:140]}», the lesson is: {lesson_t}. "
        f"Do not reuse this approach for the same intent.{tail}"
    )


def distill_apply_strategy(
    *,
    question: str,
    task_type: str,
    path_subgraph_keys: list[str],
    path_plan: Optional[list[str]],
    sql_template: str,
    card_reason: str = "",
    negative_hints: Optional[list[str]] = None,
    feedback_context: str = "",
    model: Optional[str] = None,
    skip_llm: bool = False,
    enable_thinking: Optional[bool] = None,
) -> str:
    """Success path: LLM distillation; on failure use deterministic fallback."""
    if skip_llm:
        fb = (feedback_context or "").strip()
        base = _fallback_apply(
            question,
            task_type,
            path_subgraph_keys,
            path_plan,
            sql_template,
            card_reason=card_reason,
            negative_hints=negative_hints,
        )
        if fb:
            return (base + " User feedback highlights: " + fb[:400])[:1200]
        return base

    payload: dict[str, Any] = {
        "task_type": task_type,
        "user_question": question[:800],
        "decision_rationale": (card_reason or "")[:1500],
        "negative_hints": list(negative_hints or [])[:12],
        "path_subgraph_keys": path_subgraph_keys[:24],
        "path_plan": path_plan or [],
        "sql_template": (sql_template or "")[:2000],
        "outcome": "sql_executed_successfully",
    }
    fc = (feedback_context or "").strip()
    if fc:
        payload["user_feedback_or_correction"] = fc[:1500]
    user = json.dumps(payload, ensure_ascii=False)
    try:
        out = complete_json(
            [
                {"role": "system", "content": _APPLY_SYSTEM},
                {"role": "user", "content": user},
            ],
            json_schema=STRATEGY_SEMANTICS_JSON_SCHEMA,
            model=resolve_llm_model(model),
            max_retries=CFG.agent_strategy_semantics_max_retries,
            temperature=CFG.agent_strategy_semantics_temperature,
            enable_thinking=enable_thinking,
        )
        text = str(out.get("strategy_semantics") or "").strip()
        if len(text) >= 40:
            return text[:1200]
    except Exception as exc:
        log.warning("distill_apply_strategy LLM failed: %s", exc)
    fb = (feedback_context or "").strip()
    base = _fallback_apply(
        question,
        task_type,
        path_subgraph_keys,
        path_plan,
        sql_template,
        card_reason=card_reason,
        negative_hints=negative_hints,
    )
    if fb:
        return (base + " User feedback highlights: " + fb[:400])[:1200]
    return base


def distill_avoid_strategy(
    *,
    question: str,
    task_type: str,
    path_subgraph_keys: list[str],
    lesson_or_error: str,
    skip_llm: bool = False,
    model: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
) -> str:
    """Failure path: distill avoid semantics (written to avoid cards)."""
    if skip_llm:
        return _fallback_avoid(question, task_type, path_subgraph_keys, lesson_or_error)
    user = json.dumps(
        {
            "task_type": task_type,
            "user_question": question[:800],
            "path_subgraph_keys": path_subgraph_keys[:40],
            "error_or_lesson": (lesson_or_error or "")[:1500],
        },
        ensure_ascii=False,
    )
    try:
        out = complete_json(
            [
                {"role": "system", "content": _AVOID_SYSTEM},
                {"role": "user", "content": user},
            ],
            json_schema=STRATEGY_SEMANTICS_JSON_SCHEMA,
            model=resolve_llm_model(model),
            max_retries=CFG.agent_strategy_semantics_max_retries,
            temperature=CFG.agent_strategy_semantics_temperature,
            enable_thinking=enable_thinking,
        )
        text = str(out.get("strategy_semantics") or "").strip()
        if len(text) >= 40:
            return text[:1200]
    except Exception as exc:
        log.warning("distill_avoid_strategy LLM failed: %s", exc)
    return _fallback_avoid(question, task_type, path_subgraph_keys, lesson_or_error)


def distill_feedback_strategy(
    *,
    question: str,
    task_type: str,
    path_subgraph_keys: list[str],
    sql_template: str,
    user_reason: str,
    polarity: str,
    model: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
) -> str:
    """User thumbs-down / correction: distill with polarity apply or avoid."""
    sk = not CFG.llm_strategy_semantics_distill
    if polarity == "avoid":
        base = (user_reason or "").strip() or "User reported the result did not match expectations"
        return distill_avoid_strategy(
            question=question,
            task_type=task_type,
            path_subgraph_keys=path_subgraph_keys,
            lesson_or_error=base,
            model=model,
            skip_llm=sk,
            enable_thinking=enable_thinking,
        )
    return distill_apply_strategy(
        question=question,
        task_type=task_type,
        path_subgraph_keys=path_subgraph_keys,
        path_plan=None,
        sql_template=sql_template,
        card_reason="",
        negative_hints=None,
        feedback_context=(user_reason or "").strip(),
        model=model,
        skip_llm=sk,
        enable_thinking=enable_thinking,
    )


__all__ = [
    "STRATEGY_SEMANTICS_JSON_SCHEMA",
    "distill_apply_strategy",
    "distill_avoid_strategy",
    "distill_feedback_strategy",
]
