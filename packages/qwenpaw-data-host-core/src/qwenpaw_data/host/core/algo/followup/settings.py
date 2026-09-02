# -*- coding: utf-8 -*-
"""Budgets and scoring weights for follow-up recommendation.

Plain constants, not a settings class: the algorithm reads no environment of its
own. The knobs an operator may turn are mirrored by the host's
``settings.followup.FollowUpSettings`` and arrive as arguments, so the values
here are the defaults a caller gets by saying nothing.
"""

from __future__ import annotations

COLLECTOR_QUEUE_SIZE = 2048
MIN_QUESTIONS = 2
MAX_QUESTIONS = 3
TIMEOUT_SEC = 2.0
USER_INPUT_LIMIT = 200
ANSWER_LIMIT = 300

# Two texts count as the same question above this ratio.
NOVELTY_THRESHOLD = 0.85
W_RELEVANCE = 0.4
W_EXECUTABILITY = 0.3
W_PROGRESSION = 0.3

# Entity pruning. The caps keep the prompt short enough to stay within the flash
# model's latency budget; the threshold is what keeps a domain-wide listing from
# flooding it with dimensions nobody asked about.
MAX_METRICS = 6
MAX_DIMENSIONS = 8
MIN_RELEVANCE = 0.3

# Share of an entity name's characters a question must contain for the fuzzy
# attribution pass, which recovers reorderings like "GAAP月度" for "月度GAAP".
FUZZY_COVERAGE = 0.8
FUZZY_MIN_CHARS = 3

__all__ = [
    "ANSWER_LIMIT",
    "COLLECTOR_QUEUE_SIZE",
    "FUZZY_COVERAGE",
    "FUZZY_MIN_CHARS",
    "MAX_DIMENSIONS",
    "MAX_METRICS",
    "MAX_QUESTIONS",
    "MIN_QUESTIONS",
    "MIN_RELEVANCE",
    "NOVELTY_THRESHOLD",
    "TIMEOUT_SEC",
    "USER_INPUT_LIMIT",
    "W_EXECUTABILITY",
    "W_PROGRESSION",
    "W_RELEVANCE",
]
