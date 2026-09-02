# -*- coding: utf-8 -*-
"""The FollowUpRecommend contract: start / append / join, and what the host gets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from qwenpaw_data.host.core.algo.followup import recommend as recommend_module
from qwenpaw_data.host.core.algo.followup.models import Candidate, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.recommend import FollowUpRecommend
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import ActiveModel, UserRuntimeConfig
from qwenpaw_data.host.core.runtime.context import RunContext

# Captured before the fixture below stubs the method out, so the model tests can
# still drive the real resolution.
_resolve_llm = FollowUpRecommend._build_llm


class StubLLM:
    """Answers the one call the pipeline makes, with no gateway behind it."""

    prompts: list[str] = []

    def __init__(self, model: Any, *, timeout: float) -> None:
        self.model = model
        self.timeout = timeout

    async def complete(self, prompt: str) -> dict[str, Any]:
        StubLLM.prompts.append(prompt)
        return {
            "questions": [
                {"text": "换个角度看看页面上的 GAAP用户数", "intent": "adjacent"},
                {"text": "按渠道类型拆解一下 GAAP用户数", "intent": "drilldown"},
            ]
        }


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the recommendation rule-based unless a test asks for a model."""
    monkeypatch.setattr(FollowUpRecommend, "_build_llm", lambda self: None)


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


def _run_context(light: ActiveModel | None = None) -> RunContext:
    return RunContext(
        session_id="ses-1",
        chat_id="chat-1",
        workspace=object(),
        paths=object(),
        user_runtime_config=UserRuntimeConfig(
            default=_active(model_id="qwen-plus", name="Qwen Plus"),
            light=light,
        ),
        request_context={},
        identity=Identity(user_id="u1"),
    )


def _agent_event(**payload: Any) -> dict[str, Any]:
    payload.setdefault("reply_id", "reply-1")
    return {"kind": "agent_event", "payload": payload}


def _tool_call(
    name: str, arguments: dict[str, Any], *, call_id: str
) -> list[dict[str, Any]]:
    return [
        _agent_event(
            type="TOOL_CALL_START", tool_call_id=call_id, tool_call_name=name
        ),
        _agent_event(
            type="TOOL_CALL_DELTA",
            tool_call_id=call_id,
            delta=json.dumps(arguments, ensure_ascii=False),
        ),
        _agent_event(type="TOOL_CALL_END", tool_call_id=call_id),
    ]


def _turn() -> list[dict[str, Any]]:
    """A minimal analysis: one metric fetched, one dimension left untouched."""
    context = json.dumps(
        {"schema_prompt": "指标: met:holo:DemoBiz:GAAP用户数\n  可下钻维度: 渠道类型, 页面"},
        ensure_ascii=False,
    )
    question = {"content": [{"type": "text", "text": "看下 GAAP用户数"}]}
    return [
        {"kind": "user_input", "payload": question},
        *_tool_call("search_context", {"query": "GAAP用户数"}, call_id="c1"),
        _agent_event(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="c1", delta=context),
        _agent_event(type="TOOL_RESULT_END", tool_call_id="c1", state="success"),
        *_tool_call("get_metric", {"name": "GAAP用户数"}, call_id="c2"),
        *_tool_call("get_dimension", {"name": "页面"}, call_id="c3"),
        _agent_event(type="TEXT_BLOCK_START", block_id="blk-1"),
        _agent_event(
            type="TEXT_BLOCK_DELTA", block_id="blk-1", delta="GAAP用户数 7 月环比下降 12%。"
        ),
        _agent_event(type="TEXT_BLOCK_END", block_id="blk-1"),
    ]


def _recommend(**kwargs: Any) -> FollowUpRecommend:
    kwargs.setdefault("run_context", _run_context())
    return FollowUpRecommend(**kwargs)


async def _drive(unit: FollowUpRecommend) -> list[str]:
    await unit.start()
    for entry in _turn():
        await unit.append(entry)
    await unit.append(None, last=True)
    return await unit.join()


async def test_a_driven_turn_recommends_questions() -> None:
    questions = await _drive(_recommend())

    assert 2 <= len(questions) <= 3
    assert all("GAAP用户数" in question for question in questions[:1])


def _recording_build_model(
    monkeypatch: pytest.MonkeyPatch, seen: list[ActiveModel]
) -> None:
    real = recommend_module.build_model

    def record(model: ActiveModel) -> Any:
        seen.append(model)
        return real(model)

    monkeypatch.setattr(recommend_module, "build_model", record)


async def test_the_model_channel_is_asked_and_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FollowUpRecommend, "_build_llm", _resolve_llm)
    monkeypatch.setattr(recommend_module, "FollowUpLLM", StubLLM)
    StubLLM.prompts.clear()

    unit = _recommend(max_questions=4)
    questions = await _drive(unit)

    assert "换个角度看看页面上的 GAAP用户数" in questions
    prompt = StubLLM.prompts[0]
    assert "GAAP用户数(已分析)" in prompt
    assert "bi-dimension-drilldown" in prompt


async def test_the_turn_settles_on_the_metric_the_question_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor is what every recommendation hangs off, so it has to reach
    the prompt as well as the templates."""

    monkeypatch.setattr(FollowUpRecommend, "_build_llm", _resolve_llm)
    monkeypatch.setattr(recommend_module, "FollowUpLLM", StubLLM)
    StubLLM.prompts.clear()

    await _drive(_recommend())

    assert "本轮核心指标（锚点，推荐需围绕它展开）：GAAP用户数" in StubLLM.prompts[0]


def test_the_light_model_is_what_the_model_channel_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ActiveModel] = []
    _recording_build_model(monkeypatch, seen)
    unit = _recommend(run_context=_run_context(light=_active()))

    llm = _resolve_llm(unit)

    assert [active.model_id for active in seen] == ["qwen3.7-flash"]
    assert llm is not None
    assert llm.model.stream is False
    assert llm.model.parameters.thinking_enable is False


def test_the_agents_own_model_stands_in_without_a_light_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ActiveModel] = []
    _recording_build_model(monkeypatch, seen)

    assert _resolve_llm(_recommend()) is not None
    assert [active.model_id for active in seen] == ["qwen-plus"]


def test_an_unusable_model_leaves_the_rules_channel_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_model: ActiveModel) -> Any:
        raise ValueError("api_key is required")

    monkeypatch.setattr(recommend_module, "build_model", explode)
    unit = _recommend(run_context=_run_context(light=_active(api_key="")))

    assert _resolve_llm(unit) is None


async def test_the_questions_match_the_protocol_shape() -> None:
    """``FollowUp.questions`` is what ``followup_generated`` accepts, nothing else."""

    questions = await _drive(_recommend())

    assert all(isinstance(question, str) and question for question in questions)


async def test_append_survives_anything_the_stream_carries() -> None:
    unit = _recommend()
    await unit.start()

    await unit.append({"kind": "agent_event", "payload": None})
    await unit.append({})
    await unit.append(None)
    await unit.append(None, last=True)

    assert await unit.join() == []


async def test_the_eof_sentinel_freezes_without_a_join() -> None:
    """A cancelled Chat never calls join, and must not leave a task running."""

    unit = _recommend()
    await unit.start()
    for entry in _turn():
        await unit.append(entry)
    await unit.append(None, last=True)

    snapshot = await unit._snapshot  # type: ignore[arg-type]

    assert isinstance(snapshot, SignalSnapshot)
    assert unit._pipeline is None


async def test_join_answers_the_same_way_twice() -> None:
    unit = _recommend()

    first = await _drive(unit)
    second = await unit.join()

    assert first == second
    assert second is first


async def test_join_without_start_recommends_nothing() -> None:
    assert await _recommend().join() == []


async def test_an_over_budget_recommendation_arrives_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn closes on time; the result is still worth persisting when it
    lands, so the next Chat can carry it."""

    late = Candidate(
        text="按渠道类型拆解一下 GAAP用户数",
        intent_category="drilldown",
        target_entities=[],
        source_channel="rules",
    )

    async def slow_recommend(self: Any, snapshot: SignalSnapshot) -> list[Candidate]:
        await asyncio.sleep(0.2)
        return [late]

    monkeypatch.setattr(
        recommend_module.FollowUpService, "recommend", slow_recommend
    )
    delivered: list[list[str]] = []

    async def deliver(questions: list[str]) -> None:
        delivered.append(questions)

    unit = _recommend(
        deliver=deliver, timeout_sec=0.02
    )

    assert await _drive(unit) == []

    await unit._late  # type: ignore[arg-type]
    assert delivered == [[late.text]]


async def test_a_late_result_is_dropped_when_the_host_wants_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_recommend(self: Any, snapshot: SignalSnapshot) -> list[Candidate]:
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(
        recommend_module.FollowUpService, "recommend", slow_recommend
    )
    unit = _recommend(timeout_sec=0.01)

    assert await _drive(unit) == []
    assert unit._late is None


async def test_a_failing_pipeline_costs_the_host_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(self: Any, snapshot: SignalSnapshot) -> list[Candidate]:
        raise RuntimeError("no recommendation for you")

    monkeypatch.setattr(recommend_module.FollowUpService, "recommend", explode)

    assert await _drive(_recommend()) == []
