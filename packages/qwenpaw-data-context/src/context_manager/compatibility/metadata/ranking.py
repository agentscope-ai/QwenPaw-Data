"""Rank Metadata metric candidates with lexical and vector relevance."""

from __future__ import annotations

import re
from typing import Any, Optional

from .relevance import RELEVANCE

RRF_K = 60  # classic RRF smoothing constant


def rrf_merge(
    rankings: list[list[dict[str, Any]]],
    *,
    key_field: str = "key",
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[dict[str, Any]]:
    """RRF: each ranking contributes ``w / (k + rank)`` (rank 0-based).

    ``weights`` is an optional per-ranking multiplier list (same length as
    ``rankings``). When ``None``, all rankings are weighted 1.0 (classic RRF).

    Returns rows sorted by fused score descending. First-seen row supplies base fields;
    ``score`` is the fused sum; ``rrf_components`` lists
    ``[{source: idx, rank, raw}]`` per contributing stream.
    """
    fused: dict[Any, dict[str, Any]] = {}
    for src_idx, ranking in enumerate(rankings):
        if not ranking:
            continue
        w = 1.0 if weights is None else float(weights[src_idx])
        for rank, item in enumerate(ranking):
            key = item.get(key_field)
            if key is None:
                continue
            increment = w / (k + rank)
            if key not in fused:
                fused[key] = {**item, "score": 0.0, "rrf_components": []}
            fused[key]["score"] += increment
            fused[key]["rrf_components"].append(
                {"source": src_idx, "rank": rank, "raw": item.get("score")}
            )
    return sorted(
        fused.values(),
        key=lambda x: (-x["score"], -x.get("source_trust", 1.0)),
    )


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _grams(text: str) -> set[str]:
    """Decompose a string into match grams: latin tokens (len>=2) + CJK char bigrams.

    "ChatApp 访问用户数" → {"chatapp", "访问", "问用", "用户", "户数"}.
    Single CJK chars are also added so 1-char concepts still contribute.
    """
    s = (text or "").lower().strip()
    if not s:
        return set()
    grams: set[str] = set(t for t in _TOKEN_RE.findall(s) if len(t) >= 2)
    # contiguous CJK runs → adjacent-char bigrams
    runs = re.findall(r"[\u4e00-\u9fff]+", s)
    for run in runs:
        if len(run) == 1:
            grams.add(run)
        for i in range(len(run) - 1):
            grams.add(run[i : i + 2])
    return grams


def _coverage(candidate: str, query_grams: set[str]) -> float:
    """Fraction of the candidate's grams that appear in the query (asymmetric).

    Asymmetric on purpose: we ask "how much of this short metric name/alias is
    explained by the query", not the reverse (the query is usually longer).
    """
    cg = _grams(candidate)
    if not cg or not query_grams:
        return 0.0
    inter = len(cg & query_grams)
    return inter / len(cg)


def soft_text_match(
    query: str,
    name: str,
    aliases: Optional[list[str]] = None,
    description: str = "",
) -> float:
    """Combine exact, containment and gram-coverage matches in [0, 1].

    Covering at least a quarter of a name or alias scores 0.75, allowing short
    concepts mentioned within a longer question to contribute to relevance.
    """
    q = (query or "").strip().lower()
    n = (name or "").strip().lower()
    if not q or not n:
        return 0.0

    if n == q:
        return 1.0
    als = [a.strip().lower() for a in (aliases or []) if a and a.strip()]
    if any(a == q for a in als):
        return 0.9

    # containment (kept as a strong-but-not-binary signal)
    best = 0.0
    if n in q or q in n:
        best = max(best, 0.85)
    for a in als:
        if a and (a in q or q in a):
            best = max(best, 0.6)

    qg = _grams(q)

    _ACOV_THRESH = 0.25
    _ACOV_SCORE = 0.75
    for candidate in [n] + als:
        if not candidate:
            continue
        cg = _grams(candidate)
        if not cg or not qg:
            continue
        acov = len(cg & qg) / len(cg)
        if acov >= _ACOV_THRESH:
            best = max(best, _ACOV_SCORE)

    best = max(best, _coverage(n, qg))
    for a in als:
        best = max(best, 0.9 * _coverage(a, qg))
    if description:
        best = max(best, 0.5 * _coverage(description, qg))
    return max(0.0, min(1.0, best))


def normalize_cosine(raw_vec_score: float) -> float:
    """Rescale Neo4j cosine index score ``(1+cos)/2`` ∈ [0.5,1] → [0,1].

    0.5 (orthogonal) maps to 0; 1.0 (identical) maps to 1. Values are already
    normalized for non-negative cosine; negative cosine clamps to 0.
    """
    try:
        v = float(raw_vec_score)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, (v - 0.5) * 2.0))


def blend_relevance(
    *,
    text: float,
    vec_cosine_normalized: float,
    text_weight: Optional[float] = None,
    vec_weight: Optional[float] = None,
) -> float:
    """Weighted blend of soft text match and normalized cosine, → [0, 1]."""
    tw = RELEVANCE.text_weight if text_weight is None else text_weight
    vw = RELEVANCE.vec_weight if vec_weight is None else vec_weight
    denom = (tw + vw) or 1.0
    return max(0.0, min(1.0, (tw * text + vw * vec_cosine_normalized) / denom))


def classify(
    score: float, *, threshold: Optional[float] = None, floor: Optional[float] = None
) -> str:
    th = RELEVANCE.threshold if threshold is None else threshold
    fl = RELEVANCE.floor if floor is None else floor
    if score >= th:
        return "relevant"
    if score >= fl:
        return "low_confidence"
    return "no_match"
