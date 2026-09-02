# -*- coding: utf-8 -*-
"""Narrowing candidates down to what the user actually sees.

Two compositions share the same stage functions:

* :func:`finalize_model` — for a prompt-sized model batch (already 2~3, ordered).
  Only safety gates run; model order is kept. Competitive scoring would be a
  no-op against that set.
* :func:`select` — for the rules fallback, which can emit a wider pool and still
  needs relevance / progression scoring before diversity and count bounds.
"""

from __future__ import annotations

import difflib
import logging
import re

from qwenpaw_data.host.core.algo.followup.models import (
    Candidate,
    IntentCategory,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.settings import (
    MIN_QUESTIONS,
    NOVELTY_THRESHOLD,
    W_EXECUTABILITY,
    W_PROGRESSION,
    W_RELEVANCE,
)

logger = logging.getLogger(__name__)

# Observation earns a drilldown before it earns a lateral move, so the intents
# that dig deeper outrank the ones that widen.
PROGRESSION_RANK: dict[IntentCategory, float] = {
    "drilldown": 1.0,
    "attribution": 0.9,
    "comparison": 0.7,
    "synthesis": 0.6,
    "adjacent": 0.4,
}

REPORT_ARTIFACT = "看板/报告页面"

_NORMALIZE_RE = re.compile(r"[\s\W]+", re.UNICODE)


def drop_ungrounded(
    candidates: list[Candidate], snapshot: SignalSnapshot
) -> list[Candidate]:
    """Drop questions citing no real entity, but only when entities existed.

    The condition is what makes this filter safe. When the Chat touched no
    business entity at all — raw SQL that bypassed the semantic layer, or a
    purely conversational turn — an empty attribution says nothing about the
    question, so it is kept and merely scores zero on relevance. When entities
    did exist and a question still names none, that is a reliable hallucination
    signal and worth discarding.
    """

    if not snapshot.entity_names():
        return candidates
    grounded = [candidate for candidate in candidates if candidate.target_entities]
    dropped = len(candidates) - len(grounded)
    if dropped:
        logger.warning("Dropping %d ungrounded follow-up candidate(s)", dropped)
    return grounded


def suppress_redundant_synthesis(
    candidates: list[Candidate], snapshot: SignalSnapshot
) -> list[Candidate]:
    """Drop report questions once a report or dashboard already exists.

    Both channels are filtered here: the model keeps offering to "generate a
    structured report" in a Chat that just delivered a dashboard.
    """

    if REPORT_ARTIFACT not in snapshot.artifacts_summary:
        return candidates
    return [
        candidate
        for candidate in candidates
        if candidate.intent_category != "synthesis"
    ]


def drop_near_duplicates(
    candidates: list[Candidate], snapshot: SignalSnapshot
) -> list[Candidate]:
    """Drop what repeats work already done, or an earlier candidate."""
    references = [
        _normalize(text)
        for text in (
            *snapshot.previous_followups,
            *snapshot.completed_nodes,
            *snapshot.skills_used,
        )
    ]
    kept: list[Candidate] = []
    for candidate in candidates:
        if _is_near_duplicate(candidate.text, references):
            continue
        references.append(_normalize(candidate.text))
        kept.append(candidate)
    return kept


def score_candidate(candidate: Candidate, snapshot: SignalSnapshot) -> float:
    """Score one candidate in [0, 1].

    Relevance is the mean relatedness of the entities attributed to the
    question, so a question about something peripheral loses the ranking rather
    than the delivery.
    """

    relevance_by_name = snapshot.relevance_map()
    entities = set(candidate.target_entities)
    relevance = (
        sum(relevance_by_name.get(name, 0.0) for name in entities) / len(entities)
        if entities
        else 0.0
    )
    # A templated question is known to be executable; a generated one is only
    # taken at its word.
    executability = 1.0 if candidate.source_channel == "rules" else 0.8
    progression = PROGRESSION_RANK.get(candidate.intent_category, 0.5)
    return (
        W_RELEVANCE * relevance
        + W_EXECUTABILITY * executability
        + W_PROGRESSION * progression
    )


def score_candidates(
    candidates: list[Candidate], snapshot: SignalSnapshot
) -> list[Candidate]:
    """Attach scores and return the candidates best first."""
    for candidate in candidates:
        candidate.score = score_candidate(candidate, snapshot)
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def enforce_intent_diversity(
    candidates: list[Candidate], limit: int
) -> list[Candidate]:
    """Keep at most one candidate per intent, up to ``limit`` of them.

    Expects the candidates already sorted best first, so each intent's survivor
    is its highest scorer.
    """

    picked: list[Candidate] = []
    used: set[IntentCategory] = set()
    for candidate in candidates:
        if candidate.intent_category in used:
            continue
        used.add(candidate.intent_category)
        picked.append(candidate)
        if len(picked) >= limit:
            break
    return picked


def apply_count_bounds(candidates: list[Candidate]) -> list[Candidate]:
    """Treat one survivor as none: a lone capsule is not the promised pair."""
    if len(candidates) < MIN_QUESTIONS:
        if candidates:
            logger.warning(
                "Dropping %d follow-up question(s): below the minimum of %d",
                len(candidates),
                MIN_QUESTIONS,
            )
        return []
    return candidates


def finalize_model(
    candidates: list[Candidate], snapshot: SignalSnapshot, max_questions: int
) -> list[Candidate]:
    """Keep a usable model batch without re-ranking it.

    The generation prompt already asks for 2~3 diverse, progressive questions.
    Scoring that set against itself adds little; we only drop ungrounded /
    redundant / duplicate items, keep the first of each intent in model order,
    and enforce the delivery bounds.
    """

    survivors = drop_ungrounded(candidates, snapshot)
    survivors = suppress_redundant_synthesis(survivors, snapshot)
    survivors = drop_near_duplicates(survivors, snapshot)
    survivors = enforce_intent_diversity(survivors, max_questions)
    return apply_count_bounds(survivors)


def select(
    candidates: list[Candidate], snapshot: SignalSnapshot, max_questions: int
) -> list[Candidate]:
    """Score and narrow a wider pool (rules fallback)."""
    survivors = drop_ungrounded(candidates, snapshot)
    survivors = suppress_redundant_synthesis(survivors, snapshot)
    survivors = drop_near_duplicates(survivors, snapshot)
    survivors = score_candidates(survivors, snapshot)
    survivors = enforce_intent_diversity(survivors, max_questions)
    return apply_count_bounds(survivors)


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text).lower()


def _is_near_duplicate(text: str, references: list[str]) -> bool:
    normalized = _normalize(text)
    return any(
        difflib.SequenceMatcher(None, normalized, seen).ratio() >= NOVELTY_THRESHOLD
        for seen in references
    )


__all__ = [
    "PROGRESSION_RANK",
    "REPORT_ARTIFACT",
    "apply_count_bounds",
    "drop_near_duplicates",
    "drop_ungrounded",
    "enforce_intent_diversity",
    "finalize_model",
    "score_candidate",
    "score_candidates",
    "select",
    "suppress_redundant_synthesis",
]
