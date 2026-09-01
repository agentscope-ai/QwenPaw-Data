# -*- coding: utf-8 -*-
"""Which transitions the deterministic channel offers, and when it stays quiet."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.followup.models import EntityRecord, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.rules import (
    focus_metric,
    generate_rule_candidates,
)


def _snapshot(**overrides: Any) -> SignalSnapshot:
    fields: dict[str, Any] = {
        "user_input": "看下 GAAP用户数",
        "final_answer_summary": "GAAP用户数 7 月环比下降 12%。",
        "anchor_metric": "GAAP用户数",
        "metrics": (EntityRecord(name="GAAP用户数", analyzed=True),),
        "dimensions": (EntityRecord(name="渠道类型"),),
        "unused_dimensions": ("渠道类型",),
        "artifacts_summary": "无产出物",
    }
    fields.update(overrides)
    return SignalSnapshot(**fields)


def _intents(snapshot: SignalSnapshot) -> set[str]:
    return {
        candidate.intent_category for candidate in generate_rule_candidates(snapshot)
    }


def test_an_unused_dimension_earns_a_drilldown() -> None:
    candidates = generate_rule_candidates(_snapshot())
    drilldown = next(
        candidate
        for candidate in candidates
        if candidate.intent_category == "drilldown"
    )

    assert drilldown.target_entities == ["GAAP用户数", "渠道类型"]
    assert "渠道类型" in drilldown.text


def test_no_unused_dimension_means_no_drilldown() -> None:
    assert "drilldown" not in _intents(_snapshot(unused_dimensions=()))


def test_comparison_is_suppressed_once_its_skill_was_used() -> None:
    assert "comparison" in _intents(_snapshot())
    assert "comparison" not in _intents(
        _snapshot(skills_used=("bi-comparison-analysis",))
    )


def test_attribution_waits_for_a_fluctuation_worth_explaining() -> None:
    assert "attribution" in _intents(_snapshot())
    assert "attribution" not in _intents(
        _snapshot(final_answer_summary="GAAP用户数 7 月为 1.2 万人。")
    )


def test_attribution_is_not_offered_twice() -> None:
    assert "attribution" not in _intents(
        _snapshot(completed_nodes=("波动归因: completed",))
    )
    assert "attribution" not in _intents(
        _snapshot(skills_used=("bi-attribution-analysis",))
    )


def test_synthesis_needs_two_finished_steps_and_no_html_report() -> None:
    done = ("取数: completed", "下钻: completed")

    assert "synthesis" not in _intents(_snapshot(completed_nodes=("取数: completed",)))
    assert "synthesis" not in _intents(
        _snapshot(completed_nodes=("取数: completed", "下钻: in_progress"))
    )
    candidates = generate_rule_candidates(_snapshot(completed_nodes=done))
    assert "synthesis" in {c.intent_category for c in candidates}
    assert "把本次分析整理成 HTML 报告" in {c.text for c in candidates}
    assert "synthesis" not in _intents(
        _snapshot(
            completed_nodes=done,
            artifacts_summary="已产出 1 个看板/报告页面",
        )
    )
    # Markdown alone is not an HTML report; synthesis stays on offer.
    assert "synthesis" in _intents(
        _snapshot(completed_nodes=done, artifacts_summary="已产出 1 个文档")
    )


def test_nothing_is_offered_without_a_metric_to_hang_it_on() -> None:
    assert generate_rule_candidates(_snapshot(anchor_metric="", metrics=())) == []


def test_the_users_time_window_is_carried_into_the_question() -> None:
    """A suggestion that silently widens the period reads as a different
    question than the one just answered."""

    candidates = generate_rule_candidates(
        _snapshot(user_input="看下 6月 的 GAAP用户数")
    )

    assert "按渠道类型拆解一下6月GAAP用户数" in {
        candidate.text for candidate in candidates
    }


def test_a_month_range_is_carried_whole() -> None:
    candidates = generate_rule_candidates(
        _snapshot(user_input="看下 2026年5-6月 的 GAAP用户数")
    )

    assert all("2026年5-6月" in candidate.text for candidate in candidates)


def test_the_focus_metric_is_the_anchor_the_ranking_chose() -> None:
    metrics = (
        EntityRecord(name="人均GAAP", analyzed=True),
        EntityRecord(name="GAAP用户数", analyzed=True),
    )

    assert focus_metric(_snapshot(anchor_metric="人均GAAP", metrics=metrics)) == (
        "人均GAAP"
    )


def test_a_hand_built_snapshot_falls_back_to_the_first_analyzed_metric() -> None:
    metrics = (EntityRecord(name="人均GAAP"), EntityRecord(name="GAAP用户数", analyzed=True))

    assert focus_metric(_snapshot(anchor_metric="", metrics=metrics)) == "GAAP用户数"


def test_insufficient_retrieval_skips_analysis_transitions() -> None:
    snapshot = _snapshot(intent_coverage="insufficient")

    assert _intents(snapshot) == set()


def test_every_candidate_names_only_entities_the_run_touched() -> None:
    snapshot = _snapshot(completed_nodes=("取数: completed", "下钻: completed"))
    names = snapshot.entity_names()

    for candidate in generate_rule_candidates(snapshot):
        assert set(candidate.target_entities) <= names
        assert candidate.source_channel == "rules"
