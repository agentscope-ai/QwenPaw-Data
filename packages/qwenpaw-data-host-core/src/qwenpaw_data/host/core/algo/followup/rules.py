# -*- coding: utf-8 -*-
"""The deterministic channel: analysis transitions that hold on their own.

Templates read stiffly next to a model's wording, so these are the floor and
not the goal: they cost microseconds and keep a recommendation on the screen
when the model is slow or unreachable.
"""

from __future__ import annotations

import re

from qwenpaw_data.host.core.algo.followup.models import (
    QUESTION_MAX_CHARS,
    Candidate,
    IntentCategory,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.ranking import REPORT_ARTIFACT

MIN_DONE_NODES = 2

_FLUCTUATION_WORDS = (
    "下降",
    "上升",
    "波动",
    "异常",
    "激增",
    "回落",
    "增长",
    "下滑",
    "环比",
)
# TaskStateUpdate writes ``pending`` / ``in_progress`` / ``completed``.
_DONE_SUFFIX = ": completed"
_COMPARISON_SKILL = "bi-comparison-analysis"

# The window the user stated ("6月", "5-6月", "2026年6月"), carried into the
# question so a suggestion stays inside the period they were looking at.
_TIME_WINDOW_RE = re.compile(r"(\d{4}年)?(\d{1,2}\s*[-~至]\s*\d{1,2}月|\d{1,2}月)")


def generate_rule_candidates(snapshot: SignalSnapshot) -> list[Candidate]:
    """Propose the next step for each transition the snapshot has earned."""
    candidates: list[Candidate] = []
    metric = focus_metric(snapshot)
    window = _time_window(snapshot.user_input)
    done_nodes = sum(
        1 for node in snapshot.completed_nodes if node.endswith(_DONE_SUFFIX)
    )

    retrieval_insufficient = snapshot.intent_coverage == "insufficient"

    if (
        metric
        and snapshot.unused_dimensions
        and not retrieval_insufficient
    ):
        dimension = snapshot.unused_dimensions[0]
        candidates.append(
            _candidate(
                f"按{dimension}拆解一下{window}{metric}",
                "drilldown",
                [metric, dimension],
            )
        )

    if (
        metric
        and _COMPARISON_SKILL not in snapshot.skills_used
        and not retrieval_insufficient
    ):
        candidates.append(
            _candidate(
                f"{window}{metric}环比和同比表现如何？",
                "comparison",
                [metric],
            )
        )

    fluctuated = any(
        word in snapshot.final_answer_summary for word in _FLUCTUATION_WORDS
    )
    if (
        metric
        and fluctuated
        and not _attribution_done(snapshot)
        and not retrieval_insufficient
    ):
        candidates.append(
            _candidate(
                f"{window}{metric}的波动主要由什么驱动？",
                "attribution",
                [metric],
            )
        )

    # HTML dashboard/report only (.html → 看板/报告页面). Ranking still drops
    # model-channel synthesis the same way; rules skip emitting it here.
    if (
        done_nodes >= MIN_DONE_NODES
        and REPORT_ARTIFACT not in snapshot.artifacts_summary
    ):
        candidates.append(
            _candidate(
                "把本次分析整理成 HTML 报告",
                "synthesis",
                [metric] if metric else [],
            )
        )

    return candidates


def focus_metric(snapshot: SignalSnapshot) -> str:
    """The metric the recommendations hang off.

    The anchor is chosen while ranking entities, where the user's phrasing and
    the tool traffic are both available; the first analyzed metric is only a
    fallback for a snapshot built by hand.
    """

    if snapshot.anchor_metric:
        return snapshot.anchor_metric
    analyzed = [metric.name for metric in snapshot.metrics if metric.analyzed]
    return analyzed[0] if analyzed else ""


def _time_window(user_input: str) -> str:
    match = _TIME_WINDOW_RE.search(user_input)
    if match is None:
        return ""
    return "".join(part for part in match.groups() if part)


def _candidate(
    text: str,
    intent: IntentCategory,
    entities: list[str],
) -> Candidate:
    return Candidate(
        text=text[:QUESTION_MAX_CHARS],
        intent_category=intent,
        target_entities=entities,
        source_channel="rules",
    )


def _attribution_done(snapshot: SignalSnapshot) -> bool:
    if any(
        "attribution" in skill or "归因" in skill for skill in snapshot.skills_used
    ):
        return True
    return any(
        "归因" in node or "attribution" in node for node in snapshot.completed_nodes
    )


__all__ = [
    "MIN_DONE_NODES",
    "focus_metric",
    "generate_rule_candidates",
]
