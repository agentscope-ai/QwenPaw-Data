"""Recall rerank (precision stage) for the L1 anchor set — behind a global switch.

RRF / fulltext / vector recall is tuned for *coverage*, not ordering. This stage
re-scores the top-N recalled anchors for how well they actually answer the query,
then reorders the metric / dimension / knowledge buckets in place so downstream
assembly (schema_prompt, candidate edges) surfaces the right nodes first and the
L1 response is organized by genuine relevance.

Providers:
  - ``llm``       : one JSON call scores each candidate 0–1 ("does this answer the
                    query?"). Default.
  - ``embedding`` : reuse the already-captured vector cosine (no extra model call).

Disabled by default (``CFG.rerank_enabled``). All failures degrade to a no-op so
the gate/recall behavior is never worse than before — a broken rerank just
returns ``{"enabled": True, "error": "..."}`` and the caller proceeds with the
original RRF-ranked anchor set.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..config import CFG
from ..utils import get_logger

log = get_logger("runtime.rerank")

_RERANK_BUCKETS = ("anchors_metric", "anchors_dimension", "anchors_knowledge")

_RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "score": {"type": "number", "description": "0–1 relevance to the query"},
                },
                "required": ["key", "score"],
            },
        }
    },
    "required": ["scores"],
}

_SYSTEM = """You rerank knowledge-graph candidates for an analytics query.

For each candidate (a metric / dimension / knowledge node), output a relevance
score in [0,1] for how directly it helps answer the user's question:
- 1.0: the candidate is exactly what the question asks for (e.g. the metric/dim needed)
- 0.5: plausibly related / a reasonable secondary node
- 0.0: unrelated

Judge by meaning, not surface wording (e.g. "访问趋势分析" ↔ a DAU / 活跃用户 metric is
highly relevant). Return JSON only: {"scores": [{"key": "...", "score": 0.x}, ...]}
covering every candidate key provided.
"""


def _candidate_pool(anchors: Any, top_n: int) -> list[Any]:
    """Top-N anchors across rerank buckets by current score (dedup by key)."""
    seen: set[str] = set()
    pool: list[Any] = []
    for bucket in _RERANK_BUCKETS:
        for a in getattr(anchors, bucket, []) or []:
            k = getattr(a, "key", "")
            if not k or k in seen:
                continue
            seen.add(k)
            pool.append(a)
    pool.sort(key=lambda a: -float(getattr(a, "score", 0.0) or 0.0))
    return pool[: max(1, top_n)]


def _candidate_text(a: Any) -> str:
    name = getattr(a, "name", "") or ""
    desc = (getattr(a, "description", "") or "")[:160]
    aliases = "、".join((getattr(a, "aliases", []) or [])[:5])
    parts = [p for p in (name, aliases, desc) if p]
    return " | ".join(parts)


def _llm_scores(query: str, pool: list[Any]) -> dict[str, float]:
    from ..openai_client import complete_json, resolve_llm_model

    lines = [f"- key={getattr(a, 'key', '')}: {_candidate_text(a)}" for a in pool]
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Question:\n{query}\n\nCandidates:\n" + "\n".join(lines)},
    ]
    # Rerank is a best-effort precision pass that degrades to the text+vec
    # ordering on any failure, so it must fail FAST: a single attempt with no
    # SDK-level retry storm. Otherwise a flaky chat endpoint turns one rerank
    # into minutes of exponential backoff and blocks the whole search_context.
    parsed = complete_json(
        messages,
        json_schema=_RERANK_SCHEMA,
        # Rerank is a lightweight precision judgment. An explicit ``rerank_model``
        # in config still wins, otherwise fall back to the main LLM model.
        model=CFG.rerank_model or resolve_llm_model(),
        max_retries=0,
        temperature=0.0,
        http_timeout=CFG.rerank_timeout_sec,
        client_max_retries=0,
    )
    out: dict[str, float] = {}
    for item in parsed.get("scores") or []:
        if not isinstance(item, dict):
            continue
        k = str(item.get("key") or "").strip()
        if not k:
            continue
        try:
            out[k] = max(0.0, min(1.0, float(item.get("score"))))
        except (TypeError, ValueError):
            continue
    return out


def _embedding_scores(pool: list[Any]) -> dict[str, float]:
    from .relevance import normalize_cosine

    return {
        getattr(a, "key", ""): normalize_cosine(float(getattr(a, "vec_score", 0.0) or 0.0))
        for a in pool
        if getattr(a, "key", "")
    }


def rerank_anchor_set(
    query: str,
    anchors: Any,
    *,
    embedder: Optional[Callable[[str], list[float]]] = None,
) -> dict[str, Any]:
    """Rerank + reorder anchor buckets in place. Returns a small meta dict.

    No-op (and ``{"enabled": False}``) unless ``CFG.rerank_enabled``. On any
    LLM / embedding failure returns ``{"enabled": True, "error": "..."}`` and
    leaves the buckets untouched so the caller degrades gracefully.
    """
    if not CFG.rerank_enabled:
        return {"enabled": False}

    q = (query or "").strip()
    meta: dict[str, Any] = {"enabled": True, "provider": CFG.rerank_provider, "reranked": 0}
    if not q or anchors is None:
        return {**meta, "skipped": "empty_query_or_anchors"}

    pool = _candidate_pool(anchors, CFG.rerank_top_n)
    if not pool:
        return {**meta, "skipped": "empty_pool"}

    try:
        if CFG.rerank_provider == "embedding":
            scores = _embedding_scores(pool)
        else:
            scores = _llm_scores(q, pool)
    except Exception as exc:  # noqa: BLE001
        log.warning("rerank (%s) failed: %s", CFG.rerank_provider, exc)
        return {**meta, "error": str(exc)}

    if not scores:
        return {**meta, "skipped": "no_scores"}

    w = CFG.rerank_score_weight
    n = 0
    for a in pool:
        k = getattr(a, "key", "")
        if k not in scores:
            continue
        old_norm = min(1.0, float(getattr(a, "score", 0.0) or 0.0) * 20.0)
        blended = (1.0 - w) * old_norm + w * scores[k]
        a.score = float(blended)
        # Expose the raw semantic score so downstream relevance scoring
        # (``api.semantic_pack.score_anchor``) can honor the reranker's
        # meaning-based judgment instead of recomputing from text+vec alone.
        try:
            a.rerank_score = float(scores[k])
        except (TypeError, ValueError):
            pass
        n += 1
    meta["reranked"] = n

    # Reorder buckets: reranked candidates (now on a 0–1 scale) float above the
    # untouched tail (still small RRF scores), preserving rerank order.
    for bucket in _RERANK_BUCKETS:
        lst = getattr(anchors, bucket, None)
        if isinstance(lst, list) and lst:
            lst.sort(key=lambda a: -float(getattr(a, "score", 0.0) or 0.0))

    top = sorted(scores.items(), key=lambda kv: -kv[1])[:5]
    meta["top"] = [{"key": k, "score": round(v, 3)} for k, v in top]
    return meta
