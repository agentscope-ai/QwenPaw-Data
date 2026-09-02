# -*- coding: utf-8 -*-
"""LLM-first recommendation: rules only kick in when the model cannot.

Ranking itself is covered in ``test_followup_ranking``; here the contract is
that a healthy model channel is delivered alone, and templates fill gaps on
timeout, error, or an empty/filtered model batch.
"""

from __future__ import annotations

import asyncio
from typing import Any

from qwenpaw_data.host.core.algo.followup.llm import FollowUpLLMError
from qwenpaw_data.host.core.algo.followup.models import EntityRecord, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.service import FollowUpService


class FakeLLM:
    """Stands in for the endpoint, returning or failing exactly on cue."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        error: str = "",
        delay: float = 0.0,
    ) -> None:
        self.payload = payload or {"questions": []}
        self.error = error
        self.delay = delay

    async def complete(self, prompt: str) -> dict[str, Any]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise FollowUpLLMError(self.error)
        return self.payload


def _snapshot(**overrides: Any) -> SignalSnapshot:
    fields: dict[str, Any] = {
        "user_input": "看下 GAAP用户数",
        "final_answer_summary": "GAAP用户数 7 月环比下降 12%。",
        "anchor_metric": "GAAP用户数",
        "metrics": (EntityRecord(name="GAAP用户数", analyzed=True, relevance=1.0),),
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


def _question(text: str, intent: str) -> dict[str, Any]:
    return {"text": text, "intent": intent}


def _service(llm: FakeLLM | None = None, **overrides: Any) -> FollowUpService:
    return FollowUpService(
        timeout_sec=overrides.get("timeout_sec", 2.0),
        max_questions=overrides.get("max_questions", 3),
        llm=llm,
    )


async def test_the_rules_channel_answers_when_no_model_is_configured() -> None:
    picked = await _service().recommend(_snapshot())

    assert picked
    assert {candidate.source_channel for candidate in picked} == {"rules"}


async def test_a_healthy_model_channel_is_used_alone() -> None:
    llm = FakeLLM(
        {
            "questions": [
                _question("页面上的 GAAP用户数分布如何", "adjacent"),
                _question("GAAP用户数和上月比怎样", "comparison"),
            ]
        }
    )

    picked = await _service(llm).recommend(_snapshot())

    assert len(picked) >= 2
    assert {candidate.source_channel for candidate in picked} == {"llm"}


async def test_the_questions_come_out_ranked() -> None:
    picked = await _service().recommend(_snapshot())
    scores = [candidate.score for candidate in picked]

    assert scores == sorted(scores, reverse=True)
    assert picked[0].intent_category == "drilldown"


async def test_only_one_question_per_intent_survives() -> None:
    llm = FakeLLM(
        {
            "questions": [
                _question("按渠道类型拆解 GAAP用户数", "drilldown"),
                _question("再按页面拆解 GAAP用户数", "drilldown"),
                _question("GAAP用户数的同比如何", "comparison"),
            ]
        }
    )

    picked = await _service(llm).recommend(_snapshot())
    intents = [candidate.intent_category for candidate in picked]

    assert {candidate.source_channel for candidate in picked} == {"llm"}
    assert len(intents) == len(set(intents))


async def test_the_cap_is_never_exceeded() -> None:
    picked = await _service(max_questions=2).recommend(
        _snapshot(completed_nodes=("取数: completed", "下钻: completed"))
    )

    assert len(picked) == 2


async def test_a_turn_with_nothing_to_build_on_recommends_nothing() -> None:
    snapshot = SignalSnapshot(user_input="你好")

    assert await _service().recommend(snapshot) == []


async def test_a_model_failure_falls_back_to_rules() -> None:
    picked = await _service(FakeLLM(error="502 from gateway")).recommend(_snapshot())

    assert picked
    assert {candidate.source_channel for candidate in picked} == {"rules"}


async def test_a_slow_model_falls_back_to_rules_inside_the_budget() -> None:
    """The wait is the host's, not the model's: the turn closes either way."""

    llm = FakeLLM(
        {"questions": [_question("按页面拆解 GAAP用户数", "drilldown")]}, delay=0.5
    )
    service = _service(llm, timeout_sec=0.05)

    picked = await service.recommend(_snapshot())

    assert {candidate.source_channel for candidate in picked} == {"rules"}


async def test_too_few_model_survivors_fall_back_to_rules() -> None:
    """A single LLM question cannot clear MIN_QUESTIONS; templates take over."""

    llm = FakeLLM(
        {"questions": [_question("页面上的 GAAP用户数分布如何", "adjacent")]}
    )

    picked = await _service(llm).recommend(_snapshot())

    assert picked
    assert {candidate.source_channel for candidate in picked} == {"rules"}
