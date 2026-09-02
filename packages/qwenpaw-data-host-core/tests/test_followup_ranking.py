# -*- coding: utf-8 -*-
"""Each narrowing stage on its own, and the order they run in together."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.followup import ranking
from qwenpaw_data.host.core.algo.followup.models import (
    Candidate,
    EntityRecord,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.settings import MIN_QUESTIONS

REPORT_ARTIFACT_SUMMARY = "已产出 1 个看板/报告页面"


def _snapshot(**overrides: Any) -> SignalSnapshot:
    fields: dict[str, Any] = {
        "user_input": "看下 GAAP用户数",
        "final_answer_summary": "GAAP用户数 7 月环比下降 12%。",
        "anchor_metric": "GAAP用户数",
        "metrics": (
            EntityRecord(name="GAAP用户数", analyzed=True, relevance=1.0),
        ),
        "dimensions": (
            EntityRecord(name="渠道类型", relevance=0.9),
            EntityRecord(name="页面", analyzed=True, relevance=0.4),
        ),
        "unused_dimensions": ("渠道类型",),
        "business_entities": ("GAAP用户数", "渠道类型", "页面"),
        "artifacts_summary": "无产出物",
    }
    fields.update(overrides)
    return SignalSnapshot(**fields)


def _candidate(
    text: str,
    intent: str = "drilldown",
    entities: list[str] | None = None,
    channel: str = "llm",
) -> Candidate:
    return Candidate(
        text=text,
        intent_category=intent,  # type: ignore[arg-type]
        target_entities=entities if entities is not None else ["GAAP用户数"],
        source_channel=channel,  # type: ignore[arg-type]
    )


# -- stage 1: conditional groundedness -------------------------------------- #


def test_a_question_naming_nothing_is_dropped_when_entities_existed() -> None:
    """With material to cite and nothing cited, the question was invented."""

    candidates = [_candidate("看看留存率如何", entities=[]), _candidate("拆解 GAAP用户数")]

    survivors = ranking.drop_ungrounded(candidates, _snapshot())

    assert [candidate.text for candidate in survivors] == ["拆解 GAAP用户数"]


def test_the_same_question_is_kept_when_there_was_nothing_to_cite() -> None:
    """Raw SQL bypasses the semantic layer, so an empty attribution says
    nothing about the question."""

    candidates = [_candidate("看看留存率如何", entities=[])]
    snapshot = _snapshot(metrics=(), dimensions=(), business_entities=())

    assert ranking.drop_ungrounded(candidates, snapshot) == candidates


# -- stage 2: artifact exclusion --------------------------------------------- #


def test_a_report_is_not_offered_once_one_exists() -> None:
    candidates = [
        _candidate("把本次分析整理成一份报告", "synthesis", channel="rules"),
        _candidate("整理成结构化报告", "synthesis"),
        _candidate("按渠道类型拆解 GAAP用户数"),
    ]

    survivors = ranking.suppress_redundant_synthesis(
        candidates, _snapshot(artifacts_summary=REPORT_ARTIFACT_SUMMARY)
    )

    assert [candidate.intent_category for candidate in survivors] == ["drilldown"]


def test_a_report_stands_when_the_run_produced_none() -> None:
    candidates = [_candidate("把本次分析整理成一份报告", "synthesis")]

    assert ranking.suppress_redundant_synthesis(candidates, _snapshot()) == candidates


# -- stage 3: novelty ------------------------------------------------------- #


def test_a_question_already_recommended_is_not_offered_again() -> None:
    text = "按渠道类型拆解一下 GAAP用户数"
    candidates = [_candidate(text)]

    survivors = ranking.drop_near_duplicates(
        candidates, _snapshot(previous_followups=(text,))
    )

    assert survivors == []


def test_a_question_repeating_finished_work_is_dropped() -> None:
    candidates = [_candidate("波动归因")]

    survivors = ranking.drop_near_duplicates(
        candidates, _snapshot(completed_nodes=("波动归因",))
    )

    assert survivors == []


def test_two_candidates_saying_the_same_thing_leave_one() -> None:
    candidates = [
        _candidate("按渠道类型拆解一下 GAAP用户数"),
        _candidate("按渠道类型拆解一下GAAP用户数。"),
    ]

    survivors = ranking.drop_near_duplicates(candidates, _snapshot())

    assert len(survivors) == 1


# -- stage 4: scoring ------------------------------------------------------- #


def test_a_question_about_a_more_relevant_entity_scores_higher() -> None:
    near = _candidate("按渠道类型拆解 GAAP用户数", entities=["渠道类型"])
    far = _candidate("按页面拆解 GAAP用户数", entities=["页面"])

    snapshot = _snapshot()

    assert ranking.score_candidate(near, snapshot) > ranking.score_candidate(
        far, snapshot
    )


def test_a_question_citing_nothing_scores_zero_on_relevance() -> None:
    grounded = _candidate("拆解 GAAP用户数")
    ungrounded = _candidate("看看留存率如何", entities=[])

    snapshot = _snapshot()

    assert ranking.score_candidate(grounded, snapshot) > ranking.score_candidate(
        ungrounded, snapshot
    )


def test_a_template_is_more_certain_to_execute_than_a_generated_ask() -> None:
    templated = _candidate("拆解 GAAP用户数", channel="rules")
    generated = _candidate("拆解 GAAP用户数", channel="llm")

    snapshot = _snapshot()

    assert ranking.score_candidate(templated, snapshot) > ranking.score_candidate(
        generated, snapshot
    )


def test_deeper_intents_outrank_ones_that_only_widen() -> None:
    ranks = ranking.PROGRESSION_RANK

    assert (
        ranks["drilldown"]
        > ranks["attribution"]
        > ranks["comparison"]
        > ranks["synthesis"]
        > ranks["adjacent"]
    )


def test_scoring_returns_the_candidates_best_first() -> None:
    candidates = [
        _candidate("按页面拆解 GAAP用户数", "adjacent", ["页面"]),
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown", ["渠道类型"]),
    ]

    scored = ranking.score_candidates(candidates, _snapshot())

    assert [candidate.intent_category for candidate in scored] == [
        "drilldown",
        "adjacent",
    ]


# -- stages 5 and 6: diversity and count bounds ------------------------------ #


def test_only_the_best_of_each_intent_survives() -> None:
    candidates = [
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown"),
        _candidate("按页面拆解 GAAP用户数", "drilldown"),
        _candidate("GAAP用户数的同比如何", "comparison"),
    ]

    survivors = ranking.enforce_intent_diversity(candidates, 3)

    assert [candidate.text for candidate in survivors] == [
        "按渠道类型拆解 GAAP用户数",
        "GAAP用户数的同比如何",
    ]


def test_the_cap_is_never_exceeded() -> None:
    candidates = [
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown"),
        _candidate("GAAP用户数的同比如何", "comparison"),
        _candidate("GAAP用户数的波动由什么驱动", "attribution"),
    ]

    assert len(ranking.enforce_intent_diversity(candidates, 2)) == 2


def test_a_lone_survivor_is_not_a_recommendation() -> None:
    """The product promises two or three capsules; one on its own is not a
    choice, so it is treated the same as having none."""

    assert ranking.apply_count_bounds([_candidate("拆解 GAAP用户数")]) == []


def test_the_promised_minimum_is_delivered() -> None:
    candidates = [
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown"),
        _candidate("GAAP用户数的同比如何", "comparison"),
    ]

    assert len(ranking.apply_count_bounds(candidates)) == MIN_QUESTIONS


# -- composition ------------------------------------------------------------ #


def test_finalize_model_keeps_prompt_order_without_scoring() -> None:
    """A model batch is already 2~3; competitive scoring must not reshuffle it."""

    candidates = [
        _candidate("GAAP用户数和上月比怎样", "comparison"),
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown"),
        _candidate("GAAP用户数的波动由什么驱动", "attribution"),
    ]

    survivors = ranking.finalize_model(candidates, _snapshot(), max_questions=3)

    assert [candidate.text for candidate in survivors] == [
        "GAAP用户数和上月比怎样",
        "按渠道类型拆解 GAAP用户数",
        "GAAP用户数的波动由什么驱动",
    ]
    assert all(candidate.score == 0.0 for candidate in survivors)


def test_the_pipeline_narrows_a_mixed_pool_to_distinct_intents() -> None:
    candidates = [
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown", ["渠道类型"], "rules"),
        _candidate("按渠道类型拆解一下GAAP用户数", "drilldown", ["渠道类型"]),
        _candidate("GAAP用户数的同比如何", "comparison", ["GAAP用户数"]),
        _candidate("看看留存率如何", "adjacent", []),
    ]

    survivors = ranking.select(candidates, _snapshot(), max_questions=3)

    assert [candidate.text for candidate in survivors] == [
        "按渠道类型拆解 GAAP用户数",
        "GAAP用户数的同比如何",
    ]


def test_a_pool_that_narrows_below_the_minimum_delivers_nothing() -> None:
    candidates = [
        _candidate("按渠道类型拆解 GAAP用户数", "drilldown"),
        _candidate("看看留存率如何", "adjacent", []),
    ]

    assert ranking.select(candidates, _snapshot(), max_questions=3) == []
