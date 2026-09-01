# -*- coding: utf-8 -*-
"""The one model call the recommender makes, and the model it is made on."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from qwenpaw_data.host.core.algo.followup.llm import (
    QUESTIONS_SCHEMA,
    FollowUpLLM,
    FollowUpLLMError,
    for_structured_calls,
)
from qwenpaw_data.host.core.domain.preference import ActiveModel
from qwenpaw_data.host.core.providers.factory import build_model

QUESTION: dict[str, Any] = {
    "text": "按渠道类型拆解一下 GAAP用户数",
    "intent": "drilldown",
}


@dataclass
class _Answer:
    """The one field FollowUpLLM reads off a StructuredResponse."""

    content: Any


@dataclass
class _FakeModel:
    """Stands in for a host-built chat model, recording what it was asked."""

    answer: Any
    delay: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate_structured_output(
        self, *, messages: list[Any], structured_model: Any
    ) -> _Answer:
        self.calls.append({"messages": messages, "schema": structured_model})
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Answer(self.answer)


def _llm(model: _FakeModel, *, timeout: float = 1.0) -> FollowUpLLM:
    return FollowUpLLM(model, timeout=timeout)  # type: ignore[arg-type]


def _active(**overrides: object) -> ActiveModel:
    values: dict[str, object] = {
        "provider_id": "dashscope",
        "model_id": "qwen3.7-flash",
        "api_key": "secret",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "chat_model": "DashScopeChatModel",
        "name": "Qwen3.7 Flash",
    }
    values.update(overrides)
    return ActiveModel(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("provider", ["dashscope", "openai"])
def test_a_host_built_model_is_retuned_for_generation(provider: str) -> None:
    model = for_structured_calls(build_model(_active(provider_id=provider)))

    assert model.stream is False
    assert model.parameters.thinking_enable is False
    assert model.parameters.temperature == 0.0


def test_the_schema_asks_for_nothing_the_host_can_fill_in() -> None:
    """Entities and skills are recovered locally, and every token the model
    does not have to decode comes straight off the turn's latency."""

    item = QUESTIONS_SCHEMA["properties"]["questions"]["items"]

    assert set(item["properties"]) == {"text", "intent"}
    assert item["required"] == ["text", "intent"]


async def test_the_call_carries_the_prompt_and_the_schema() -> None:
    model = _FakeModel({"questions": [QUESTION]})

    data = await _llm(model).complete("推荐追问")

    assert data == {"questions": [QUESTION]}
    assert model.calls[0]["schema"] is QUESTIONS_SCHEMA
    messages = model.calls[0]["messages"]
    assert [(msg.role, msg.get_text_content()) for msg in messages] == [
        ("user", "推荐追问")
    ]


async def test_questions_sent_as_text_are_decoded() -> None:
    """小模型常把数组塞成字符串；这与它写成数组是同一个值。"""

    model = _FakeModel({"questions": json.dumps([QUESTION], ensure_ascii=False)})

    assert await _llm(model).complete("推荐追问") == {"questions": [QUESTION]}


async def test_text_that_only_looks_like_a_list_is_left_alone() -> None:
    model = _FakeModel({"questions": "没有可推荐的追问"})

    assert await _llm(model).complete("推荐追问") == {
        "questions": "没有可推荐的追问"
    }


async def test_an_answer_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(FollowUpLLMError):
        await _llm(_FakeModel("抱歉，我没有推荐")).complete("推荐追问")


async def test_a_broken_model_is_reported_not_retried() -> None:
    """The budget belongs to the turn, so there is no room for a second try."""

    model = _FakeModel(RuntimeError("gateway is down"))

    with pytest.raises(FollowUpLLMError, match="gateway is down"):
        await _llm(model).complete("推荐追问")

    assert len(model.calls) == 1


async def test_a_stalled_call_gives_up_at_this_turn_budget() -> None:
    llm = _llm(_FakeModel({"questions": []}, delay=1.0), timeout=0.02)

    with pytest.raises(FollowUpLLMError):
        await llm.complete("推荐追问")
