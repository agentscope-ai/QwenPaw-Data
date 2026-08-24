"""Shared relevance scoring for L1/L2 gates (search_context / search_event / explore_entity).

Why this module exists
----------------------
The old gate blended ``0.6 * substring_match + 0.4 * (rrf_score * 20)``:

- ``substring_match`` was a hard containment test, so any semantic paraphrase
  (e.g. "访问趋势分析" vs metric "DAU"/alias "访问用户数") scored 0.
- RRF is a *rank* fusion and discards absolute similarity; its top value for two
  streams is ``1/60 + 1/60 ≈ 0.033``. Scaled ``*20 *0.4`` that caps a pure-semantic
  hit at ~0.27 — structurally below the 0.40 threshold. No embedding hit could pass.

This module fixes both:

- :func:`soft_text_match` does CJK-bigram + token overlap (partial credit), not
  substring-only.
- :func:`blend_relevance` uses the *raw vector cosine* (rescaled from Neo4j's
  ``(1+cos)/2`` range) as the dense signal, with vector weighted above text by default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..config import CFG

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
    """Soft lexical match in [0, 1] tolerant to CJK paraphrase.

    - exact name/alias == query → 1.0
    - alias exact == query → 0.9
    - whole name/alias contained in query (or vice versa) → 0.85 / 0.6
    - **alias/name gram coverage by query ≥ 25% → 0.75** (see below)
    - otherwise CJK-bigram + token coverage of name (full weight) / aliases (0.9) /
      description (0.5), taking the max.

    Alias-coverage gate (NEW):
        ``acov = |candidate_grams ∩ query_grams| / |candidate_grams| ≥ 0.25``
        → score 0.75.

        Catches the common case where the user's query *mentions the core
        concept* of an alias but is phrased around it (e.g. query
        ``"访问趋势分析"`` vs alias ``"访问用户数"`` — ``"访问"`` is the
        concept, ``"趋势分析"`` is the task the user wants to do with it).
        The old ``a in q`` containment fails because the alias isn't a
        contiguous substring of the query; the old asymmetric
        ``_coverage(a, qg)`` only gives 0.25 because 3 of the alias's 4
        bigrams (``问用/用户/户数``) don't appear in the query.

        acov is the asymmetric direction that discriminates: for the short
        alias ``"访问用户数"`` (4 bigrams), 1/4 = 0.25 ≥ 0.25 → fires. For
        the longer concrete metric ``"分享页访问用户数"`` (7 bigrams), 1/7 ≈
        0.14 < 0.25 → does NOT fire, so the metric stays correctly low-scored.
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

    # Alias/name gate: if the query covers a meaningful fraction of the
    # candidate's own grams (acov = |A∩Q| / |A| ≥ 0.25), treat it as a
    # strong conceptual match. This is the asymmetric direction that
    # matters for "query mentions the core concept of a short alias":
    #
    #   query "ChatApp 12 月访问趋势分析" (6 CJK bigrams)
    #   alias "访问用户数"  → 4 bigrams, share {访问} → acov 1/4 = 0.25 → fires
    #   name  "分享页访问用户数" → 7 bigrams, share {访问} → acov 1/7 ≈ 0.14 → does NOT
    #
    # The old asymmetric ``_coverage(a, qg)`` only gave 0.9 * 0.25 = 0.225
    # for DAU (because ``a in q`` containment failed), letting the more
    # specific ``分享页访问用户数`` win via a small vector boost. Promoting
    # the acov ≥ 0.25 case to 0.75 gives the alias its due weight without
    # an LLM rerank round-trip.
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


@dataclass(frozen=True)
class RelevanceVerdict:
    status: str        # relevant | low_confidence | no_match
    score: float       # blended 0–1
    matched_name: str
    text: float = 0.0
    vec: float = 0.0


def blend_relevance(
    *,
    text: float,
    vec_cosine_normalized: float,
    text_weight: Optional[float] = None,
    vec_weight: Optional[float] = None,
) -> float:
    """Weighted blend of soft text match and normalized cosine, → [0, 1]."""
    tw = CFG.relevance_text_weight if text_weight is None else text_weight
    vw = CFG.relevance_vec_weight if vec_weight is None else vec_weight
    denom = (tw + vw) or 1.0
    return max(0.0, min(1.0, (tw * text + vw * vec_cosine_normalized) / denom))


def classify(score: float, *, threshold: Optional[float] = None, floor: Optional[float] = None) -> str:
    th = CFG.relevance_threshold if threshold is None else threshold
    fl = CFG.relevance_floor if floor is None else floor
    if score >= th:
        return "relevant"
    if score >= fl:
        return "low_confidence"
    return "no_match"


def score_candidate(
    query: str,
    *,
    name: str,
    aliases: Optional[list[str]] = None,
    description: str = "",
    raw_vec_score: float = 0.0,
    text_weight: Optional[float] = None,
    vec_weight: Optional[float] = None,
) -> tuple[float, float, float]:
    """Return ``(blended, text, vec_normalized)`` for one candidate."""
    text = soft_text_match(query, name, aliases, description)
    vecn = normalize_cosine(raw_vec_score)
    blended = blend_relevance(
        text=text,
        vec_cosine_normalized=vecn,
        text_weight=text_weight,
        vec_weight=vec_weight,
    )
    return blended, text, vecn
