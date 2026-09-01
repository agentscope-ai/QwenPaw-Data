# -*- coding: utf-8 -*-
"""What the model is told, and what is made of the two fields it answers with."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.followup.generator import (
    PROMPT_TEMPLATE_PATH,
    parse_candidates,
    render_prompt,
)
from qwenpaw_data.host.core.algo.followup.models import EntityRecord, SignalSnapshot


def _snapshot(**overrides: Any) -> SignalSnapshot:
    fields: dict[str, Any] = {
        "user_input": "看下 6月 的 GAAP用户数",
        "final_answer_summary": "GAAP用户数 6 月环比下降 12%。",
        "completed_nodes": ("取数: completed",),
        "skills_used": ("bi-metric-observation",),
        "anchor_metric": "GAAP用户数",
        "metrics": (EntityRecord(name="GAAP用户数", analyzed=True, relevance=1.0),),
        "dimensions": (EntityRecord(name="渠道类型", relevance=0.9),),
        "datasets": (EntityRecord(name="dwd_gaap_daily", analyzed=True),),
        "unused_dimensions": ("渠道类型",),
        "business_entities": ("GAAP用户数", "渠道类型"),
        "artifacts_summary": "无产出物",
        "previous_followups": ("6月GAAP用户数环比如何",),
    }
    fields.update(overrides)
    return SignalSnapshot(**fields)


def test_every_placeholder_is_filled_in() -> None:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    prompt = render_prompt(_snapshot())

    # The JSON example keeps its own braces, so only the named slots count.
    for line in template.splitlines():
        for fragment in line.split("{")[1:]:
            name = fragment.split("}")[0]
            if name.isidentifier():
                assert "{" + name + "}" not in prompt


def test_the_prompt_carries_the_anchor_and_the_material() -> None:
    prompt = render_prompt(_snapshot())

    assert "GAAP用户数" in prompt
    assert "渠道类型" in prompt
    assert "bi-metric-observation" in prompt
    assert "6月GAAP用户数环比如何" in prompt
    assert "本轮已命中已验证 SQL" in render_prompt(
        _snapshot(has_golden_query=True)
    )
    assert "insufficient" in render_prompt(_snapshot(intent_coverage="insufficient"))


def test_a_table_name_never_reaches_the_prompt() -> None:
    """A physical table is internal plumbing: naming it invites a suggestion
    the user cannot read and the router cannot route."""

    assert "dwd_gaap_daily" not in render_prompt(_snapshot())


def test_an_empty_snapshot_still_renders() -> None:
    prompt = render_prompt(SignalSnapshot())

    assert "无" in prompt


def test_the_entities_are_recovered_from_the_question_text() -> None:
    payload = {
        "questions": [
            {"text": "按渠道类型拆解一下 GAAP用户数", "intent": "drilldown"}
        ]
    }

    candidates = parse_candidates(payload, _snapshot())

    assert candidates[0].target_entities == ["GAAP用户数", "渠道类型"]
    assert candidates[0].source_channel == "llm"


def test_an_alias_in_the_question_is_recorded_as_the_canonical_name() -> None:
    payload = {"questions": [{"text": "人均消费的趋势如何", "intent": "adjacent"}]}
    snapshot = _snapshot(
        business_entities=("人均GAAP",),
        entity_aliases=(("人均消费", "人均GAAP"),),
    )

    candidates = parse_candidates(payload, snapshot)

    assert candidates[0].target_entities == ["人均GAAP"]


def test_a_report_question_inherits_the_anchor() -> None:
    """"把本次分析整理成一份报告" names no entity by nature, and would look
    invented under the groundedness filter without this."""

    payload = {
        "questions": [{"text": "把本次分析整理成一份报告", "intent": "synthesis"}]
    }

    candidates = parse_candidates(payload, _snapshot())

    assert candidates[0].target_entities == ["GAAP用户数"]


def test_only_a_synthesis_question_is_exempt() -> None:
    payload = {"questions": [{"text": "看看留存率如何", "intent": "adjacent"}]}

    candidates = parse_candidates(payload, _snapshot())

    assert candidates[0].target_entities == []


def test_one_malformed_question_does_not_cost_the_batch() -> None:
    """The call has no second attempt inside the turn budget."""

    payload = {
        "questions": [
            {"text": "", "intent": "drilldown"},
            {"text": "按渠道类型拆解 GAAP用户数", "intent": "made-up"},
            "不是一个对象",
            {"text": "按渠道类型拆解一下 GAAP用户数", "intent": "drilldown"},
        ]
    }

    candidates = parse_candidates(payload, _snapshot())

    assert [candidate.text for candidate in candidates] == [
        "按渠道类型拆解一下 GAAP用户数"
    ]


def test_a_payload_without_questions_yields_nothing() -> None:
    assert parse_candidates({"answer": "没有推荐"}, _snapshot()) == []
