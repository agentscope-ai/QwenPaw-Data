"""Trust scores for doc-ingested facts (MG cap vs manual metrics_dict)."""
from __future__ import annotations

import re
from typing import Optional

# MG-derived nodes from LLM must stay below manual YAML trust (0.9) so CONFLICT wins over SUPERSEDE.
MG_TRUST_CAP = 0.7

_DOC_PRIORS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"月报|取数|操作手册|运营", re.I), 0.8),
    (re.compile(r"竞品|对比|benchmark|competitor", re.I), 0.6),
    (re.compile(r"\.pdf|分析|调研|访谈|趋势", re.I), 0.5),
]


def doc_class_prior(source_doc: str) -> float:
    s = source_doc or ""
    for pat, p in _DOC_PRIORS:
        if pat.search(s):
            return p
    return 0.65


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def source_trust(
    *,
    source_doc: str,
    self_confidence: float,
    repeat_count: int = 1,
    is_mg_candidate: bool = False,
) -> float:
    """trust = prior * clamp(self_confidence) * repeat_bonus; MG rows capped."""
    prior = doc_class_prior(source_doc)
    base = prior * clamp01(self_confidence)
    bonus = 1.0 + 0.05 * max(0, int(repeat_count) - 1)
    t = min(0.85, base * bonus)
    if is_mg_candidate:
        t = min(MG_TRUST_CAP, t)
    return round(t, 4)
