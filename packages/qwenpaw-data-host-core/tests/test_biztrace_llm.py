# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from qwenpaw_data.host.core.algo.biztrace import pipeline
from qwenpaw_data.host.core.algo.biztrace.llm import (
    StructuredLLM,
    StructuredLLMError,
    _tolerant_schema,
    for_structured_calls,
)
from qwenpaw_data.host.core.algo.biztrace.segmentation import _EXTRACTION_SCHEMA
from qwenpaw_data.host.core.domain.preference import ActiveModel
from qwenpaw_data.host.core.providers.factory import build_model

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
}


@dataclass
class _Answer:
    """The one field StructuredLLM reads off a StructuredResponse."""

    content: Any


@dataclass
class _FakeModel:
    """Stands in for a host-built chat model, recording what it was asked."""

    answers: list[Any]
    delay: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate_structured_output(
        self, *, messages: list[Any], structured_model: Any
    ) -> _Answer:
        self.calls.append({"messages": messages, "schema": structured_model})
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return _Answer(answer)


def _llm(model: _FakeModel, *, timeout: float = 1.0) -> StructuredLLM:
    return StructuredLLM(model, timeout=timeout)  # type: ignore[arg-type]


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
def test_a_host_built_model_is_retuned_for_extraction(provider: str) -> None:
    model = for_structured_calls(build_model(_active(provider_id=provider)))

    assert model.stream is False
    assert model.parameters.thinking_enable is False
    assert model.parameters.temperature == 0.0
    # The host's own choice is left alone: one forced tool call at a time.
    assert model.parameters.parallel_tool_calls is False


async def test_a_call_carries_the_prompt_and_the_schema() -> None:
    model = _FakeModel([{"body": "取数完成"}])
    llm = _llm(model)

    data = await llm.complete(
        system="s", user="u", schema=SCHEMA, schema_name="presentation_body"
    )

    assert data == {"body": "取数完成"}
    assert model.calls[0]["schema"] is SCHEMA
    roles = [(msg.role, msg.get_text_content()) for msg in model.calls[0]["messages"]]
    assert roles == [("system", "s"), ("user", "u")]


async def test_a_first_bad_answer_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qwenpaw_data.host.core.algo.biztrace.llm._BACKOFF_BASE_SECONDS", 0)
    model = _FakeModel([RuntimeError("no tool call"), {"body": "ok"}])
    llm = _llm(model)

    data = await llm.complete(system="s", user="u", schema=SCHEMA, schema_name="body")

    assert data == {"body": "ok"}
    assert len(model.calls) == 2
    assert llm.failures == 0


async def test_a_retry_tells_the_model_what_was_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """温度为 0，重问同一句只会得到同一个坏答案，必须把错误告诉模型。"""

    monkeypatch.setattr("qwenpaw_data.host.core.algo.biztrace.llm._BACKOFF_BASE_SECONDS", 0)
    model = _FakeModel([RuntimeError("'x' is not of type 'array'"), {"body": "ok"}])
    llm = _llm(model)

    await llm.complete(system="s", user="u", schema=SCHEMA, schema_name="body")

    retry = [msg.get_text_content() for msg in model.calls[1]["messages"]]
    assert len(retry) == 3
    assert "'x' is not of type 'array'" in retry[2]


async def test_a_nested_container_sent_as_text_is_decoded() -> None:
    """小模型常把嵌套数组塞成字符串；这与它写成数组是同一个值。"""

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "artifact": {"type": ["array", "null"], "items": {"type": "object"}},
        },
        "required": ["title"],
    }
    model = _FakeModel(
        [{"title": "产出报告", "artifact": '[{"name": "a.md"}]'}]
    )

    data = await _llm(model).complete(
        system="s", user="u", schema=schema, schema_name="segment_metadata"
    )

    assert data == {"title": "产出报告", "artifact": [{"name": "a.md"}]}
    # The model is asked for an array and only *accepts* the string form, so
    # the transport never rejects the payload before it can be repaired.
    accepted = model.calls[0]["schema"]["properties"]
    assert accepted["artifact"]["type"] == ["array", "null", "string"]
    assert accepted["title"]["type"] == "string"


async def test_the_reported_extraction_failure_now_survives_the_round_trip() -> None:
    """线上真实样本：模型把 artifact 整个数组塞成一串 JSON 文本。

    真正兜住这个失败的是传输层，不是提示词：出事时提示词已经两处写明
    ``artifact (array | null)``，模型照样交了字符串。这里按真实 schema 钉住
    「provider 端能过、交回流水线时又是数组」这条不变量。
    """

    artifacts = [
        {
            "name": "user_behavior_analysis.py",
            "description": "用户行为数据分析脚本",
            "kind": "query_script",
            "role": "supporting",
        },
        {
            "name": "visualize_analysis.py",
            "description": "生成可视化图表的脚本",
            "kind": "query_script",
            "role": "supporting",
        },
    ]
    payload = {
        "title": "分析用户行为并出图",
        "input": None,
        "behavior": "1. 读取行为明细\n2. 汇总后出图",
        "conclusion": "活跃用户集中在晚间。",
        "artifact": json.dumps(artifacts, ensure_ascii=False),
    }

    # This is the exact rejection that was reported.
    with pytest.raises(jsonschema.ValidationError, match="is not of type"):
        jsonschema.validate(payload, _EXTRACTION_SCHEMA)

    # The schema the provider validates against accepts the stringified form,
    # so the payload reaches us instead of failing inside the transport.
    jsonschema.validate(payload, _tolerant_schema(_EXTRACTION_SCHEMA))

    data = await _llm(_FakeModel([payload])).complete(
        system="s",
        user="u",
        schema=_EXTRACTION_SCHEMA,
        schema_name="segment_metadata",
    )

    # And what the pipeline gets back conforms to the strict schema again.
    jsonschema.validate(data, _EXTRACTION_SCHEMA)
    assert data["artifact"] == artifacts


async def test_text_that_only_looks_like_a_container_is_left_alone() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"artifact": {"type": ["array", "null"]}},
    }
    model = _FakeModel([{"artifact": "没有产出文件"}])

    data = await _llm(model).complete(
        system="s", user="u", schema=schema, schema_name="segment_metadata"
    )

    assert data == {"artifact": "没有产出文件"}


async def test_an_answer_that_is_not_an_object_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("qwenpaw_data.host.core.algo.biztrace.llm._BACKOFF_BASE_SECONDS", 0)
    llm = _llm(_FakeModel(["not an object"]))

    with pytest.raises(StructuredLLMError):
        await llm.complete(system="s", user="u", schema=SCHEMA, schema_name="body")

    assert llm.failures == 1


async def test_a_broken_model_raises_after_its_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("qwenpaw_data.host.core.algo.biztrace.llm._BACKOFF_BASE_SECONDS", 0)
    model = _FakeModel([RuntimeError("gateway is down")])
    llm = _llm(model)

    with pytest.raises(StructuredLLMError, match="gateway is down"):
        await llm.complete(system="s", user="u", schema=SCHEMA, schema_name="body")

    assert len(model.calls) == llm.attempts
    assert llm.failures == 1


async def test_a_stalled_call_gives_up_at_this_step_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """算法侧的超时是这一步自己的预算，慢模型不能拖住整轮回复。"""

    monkeypatch.setattr("qwenpaw_data.host.core.algo.biztrace.llm._BACKOFF_BASE_SECONDS", 0)
    llm = _llm(_FakeModel([{"body": "ok"}], delay=1.0), timeout=0.02)

    with pytest.raises(StructuredLLMError):
        await llm.complete(system="s", user="u", schema=SCHEMA, schema_name="body")

    assert llm.failures == 1


@dataclass
class _RecordedClient:
    """Stands in for StructuredLLM to record what each step was given."""

    model: Any
    timeout: float


def _record_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[_RecordedClient]:
    monkeypatch.setenv("QWENPAW_DATA_BIZ_LINK_ENABLED", "false")
    monkeypatch.setenv("QWENPAW_DATA_BIZ_TRACE_LOG_DIR", str(tmp_path))
    built: list[_RecordedClient] = []

    def _client(model: Any, *, timeout: float) -> _RecordedClient:
        client = _RecordedClient(model, timeout)
        built.append(client)
        return client

    monkeypatch.setattr(pipeline, "StructuredLLM", _client)
    return built


async def test_every_step_shares_one_model_and_differs_only_in_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _record_clients(monkeypatch, tmp_path)
    model = _FakeModel([{"body": "ok"}])

    made = pipeline.build_pipeline(
        session_id="ses_1",
        chat_model=model,  # type: ignore[arg-type]
    )

    assert made is not None
    assert {id(client.model) for client in built} == {id(model)}
    assert sorted(client.timeout for client in built) == [5.0, 30.0, 60.0]
    await made.aclose()


async def test_a_run_without_a_model_stays_rule_based(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _record_clients(monkeypatch, tmp_path)

    made = pipeline.build_pipeline(session_id="ses_1")

    assert made is not None
    assert built == []
    await made.aclose()
