"""Topology pipeline: LLM splits retrieval text into facet phrases for multi-query recall."""
from __future__ import annotations

from typing import Any, Optional

from ..config import CFG
from ..openai_client import complete_json, resolve_llm_model
from ..utils import get_logger

log = get_logger("runtime.semantic_split")

SEMANTIC_SPLIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Standalone retrieval phrases for metrics, dimensions, columns, entities, "
                "product/platform filters — no calendar dates or time-window text. "
                "Each phrase may lightly inline likely synonyms or English/abbrev catalog spellings "
                "(same line, few tokens) to improve fulltext/vector hit rate. "
                "Each phrase must be self-contained for graph fulltext/vector search."
            ),
        },
    },
    "required": ["facets"],
}

SYSTEM_PROMPT = """Extract short, standalone retrieval phrases from the user's question for knowledge-graph lookup.

Rules:
- Strip all time references (dates, 近N日, 上周, quarters, etc.) before forming phrases.
- One phrase per concept: metric, dimension, entity, or platform scope.
- After each main phrase, append 1–3 synonyms / English tokens / catalog abbreviations (space-separated) to improve graph matching. Do not add concepts not in the question.
- Prefer 2–6 phrases for compound questions; 1 if already atomic.
- Do NOT include the domain name in facet phrases. Domain scoping is handled
  separately as a post-retrieval filter, not as a search token.
- Output only: {"facets": ["phrase1", "phrase2", ...]}

Example: {"facets": ["DAU 日活 daily active users", "iOS平台 iOS", "次日留存率 day1 retention", "广告收入 ad revenue"]}
"""


def merge_strategy_card_candidates(
    per_facet: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Merge card recall lists: same card key keeps row with highest composite_score."""
    by_key: dict[str, dict[str, Any]] = {}
    for rows in per_facet:
        for c in rows or []:
            k = (c or {}).get("key")
            if not k:
                continue
            ks = str(k)
            prev = by_key.get(ks)
            sc = float((c or {}).get("composite_score") or 0.0)
            if prev is None or sc > float((prev or {}).get("composite_score") or 0.0):
                by_key[ks] = dict(c)
    merged = list(by_key.values())
    merged.sort(key=lambda x: -float((x or {}).get("composite_score") or 0.0))
    return merged


def semantic_split_for_retrieval(
    effective_retrieval_q: str,
    cfg: Any,
    *,
    model: Optional[str] = None,
    reasoning_capture: Optional[list[str]] = None,
    metadata_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
    domain: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """Return ``(facets, meta)``. On skip/failure, ``facets`` is ``[stripped effective query]``."""
    meta: dict[str, Any] = {"fallback": False, "skipped": False}
    base = (effective_retrieval_q or "").strip()
    if not getattr(cfg, "semantic_split_retrieval", False):
        meta["skipped"] = True
        meta["reason"] = "semantic_split_retrieval_false"
        return ([base] if base else [""]), meta

    if not base:
        meta["skipped"] = True
        meta["reason"] = "empty_query"
        return [""], meta

    max_n = int(getattr(cfg, "semantic_split_max_facets", 8) or 8)
    max_n = min(max_n, CFG.recall_semantic_split_max_facets)
    max_n = max(1, min(max_n, 32))

    user_content = f"Question for retrieval:\n{base}"
    if domain:
        user_content = f"Domain scope: {domain}\n\n{user_content}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        parsed = complete_json(
            messages,
            json_schema=SEMANTIC_SPLIT_SCHEMA,
            model=resolve_llm_model(model or getattr(cfg, "llm_model", None)),
            max_retries=CFG.agent_semantic_split_max_retries,
            temperature=CFG.agent_semantic_split_temperature,
            reasoning_capture=reasoning_capture,
            metadata_out=metadata_out,
            enable_thinking=enable_thinking,
        )
        raw = parsed.get("facets") or []
        facets = []
        for x in raw:
            s = str(x).strip()
            if len(s) >= 2:
                facets.append(s)
        facets = facets[:max_n]
        if not facets:
            raise ValueError("empty facets after parse")
        meta["llm_called"] = True
        meta["n_facets"] = len(facets)
        if metadata_out:
            meta["llm"] = dict(metadata_out)
        return facets, meta
    except Exception as exc:
        log.warning("semantic_split_for_retrieval failed: %s", exc)
        meta["fallback"] = True
        meta["error"] = str(exc)
        return [base], meta
