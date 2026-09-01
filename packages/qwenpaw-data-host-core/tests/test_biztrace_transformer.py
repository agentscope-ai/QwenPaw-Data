# -*- coding: utf-8 -*-
"""The Transformer contract: start / append / join, and what reaches the host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from qwenpaw_data.host.core.algo.biztrace import transformer as transformer_module
from qwenpaw_data.host.core.algo.biztrace.transformer import BizTraceTransformer
from qwenpaw_data.host.core.api.models.trace import (
    BizEventSchema,
    SegmentArtifactSchema,
    SegmentSchema,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import ActiveModel, UserRuntimeConfig
from qwenpaw_data.host.core.runtime.context import RunContext

# Captured before the fixture below stubs the method out, so the model tests can
# still drive the real resolution.
_resolve_chat_model = BizTraceTransformer._chat_model


class FakeEnvelope:
    """Records what the algorithm sends, with the Envelope's exact signatures."""

    def __init__(self) -> None:
        self.biz_events: list[dict[str, Any]] = []
        self.segments: list[dict[str, Any]] = []

    async def send_biz_event(self, biz_event: dict[str, Any]) -> None:
        self.biz_events.append(biz_event)

    async def send_segment(self, segment: dict[str, Any]) -> None:
        self.segments.append(segment)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the pipeline rule-based: no model calls, no vocabulary fetch."""
    monkeypatch.setenv("QWENPAW_DATA_BIZ_LINK_ENABLED", "false")
    monkeypatch.setenv("QWENPAW_DATA_TRACE2SEGMENT_ENABLED", "false")
    monkeypatch.setattr(BizTraceTransformer, "_chat_model", lambda self: None)


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


def _run_context(
    tmp_path: Path,
    *,
    light: ActiveModel | None = None,
    default: ActiveModel | None = None,
) -> RunContext:
    from qwenpaw_data.host.core.paths import Paths

    return RunContext(
        session_id="ses-1",
        chat_id="chat-1",
        workspace=object(),
        paths=Paths(home=tmp_path, session_id="ses-1"),
        user_runtime_config=UserRuntimeConfig(
            default=default or _active(model_id="qwen-plus", name="Qwen Plus"),
            light=light,
        ),
        request_context={"datasource_id": "ds-1"},
        identity=Identity(user_id="u1"),
    )


def _transformer(
    tmp_path: Path, envelope: FakeEnvelope, *, light: ActiveModel | None = None
) -> BizTraceTransformer:
    return BizTraceTransformer(
        run_context=_run_context(tmp_path, light=light),
        envelope=envelope,  # type: ignore[arg-type]
    )


def _agent_event(**payload: Any) -> dict[str, Any]:
    payload.setdefault("reply_id", "reply-1")
    return {"kind": "agent_event", "payload": payload}


def _text_block(block_id: str, text: str) -> list[dict[str, Any]]:
    return [
        _agent_event(type="TEXT_BLOCK_START", block_id=block_id),
        _agent_event(type="TEXT_BLOCK_DELTA", block_id=block_id, delta=text),
        _agent_event(type="TEXT_BLOCK_END", block_id=block_id),
    ]


async def _drive(
    transformer: BizTraceTransformer, entries: list[dict[str, Any]]
) -> None:
    await transformer.start()
    for entry in entries:
        await transformer.append(entry)  # type: ignore[arg-type]
    await transformer.append(None, last=True)
    await transformer.join()


def _recording_build_model(
    monkeypatch: pytest.MonkeyPatch, seen: list[ActiveModel]
) -> None:
    real = transformer_module.build_model

    def record(model: ActiveModel) -> Any:
        seen.append(model)
        return real(model)

    monkeypatch.setattr(transformer_module, "build_model", record)


def test_the_light_model_is_what_conversion_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ActiveModel] = []
    _recording_build_model(monkeypatch, seen)
    transformer = _transformer(tmp_path, FakeEnvelope(), light=_active())

    model = _resolve_chat_model(transformer)

    assert [active.model_id for active in seen] == ["qwen3.7-flash"]
    assert model is not None
    assert model.stream is False
    assert model.parameters.thinking_enable is False


def test_the_agents_own_model_stands_in_without_a_light_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ActiveModel] = []
    _recording_build_model(monkeypatch, seen)
    transformer = _transformer(tmp_path, FakeEnvelope(), light=None)

    assert _resolve_chat_model(transformer) is not None
    assert [active.model_id for active in seen] == ["qwen-plus"]


def test_an_unusable_model_leaves_the_turn_rule_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_model: ActiveModel) -> Any:
        raise ValueError("api_key is required")

    monkeypatch.setattr(transformer_module, "build_model", explode)
    transformer = _transformer(tmp_path, FakeEnvelope(), light=_active(api_key=""))

    assert _resolve_chat_model(transformer) is None


async def test_a_driven_turn_sends_its_cards(tmp_path: Path) -> None:
    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)

    await _drive(transformer, _text_block("blk-1", "日活在月中下降。"))

    assert [event["block_id"] for event in envelope.biz_events] == ["blk-1"]
    assert envelope.biz_events[0]["status"] == "done"
    assert envelope.biz_events[0]["presentation"]["card_type"] == "text"


async def test_the_payload_matches_what_the_stream_accepts(
    tmp_path: Path,
) -> None:
    """A stray key would only fail at ``stream.biz_event(**payload)``."""
    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)

    await _drive(transformer, _text_block("blk-1", "结论"))

    accepted = set(BizEventSchema.model_fields)
    assert set(envelope.biz_events[0]) <= accepted - {"chat_id"}


def test_the_segment_projection_matches_the_stream_schema() -> None:
    from qwenpaw_data.host.core.algo.biztrace.models import (
        FrontendArtifact,
        FrontendSegment,
    )

    accepted = set(SegmentSchema.model_fields)
    assert set(FrontendSegment.model_fields) <= accepted - {"chat_id"}
    # The host's segment schema forbids unknown keys, so an artifact crosses
    # over as name / description / relative_path and nothing else.
    assert set(FrontendArtifact.model_fields) <= set(
        SegmentArtifactSchema.model_fields
    )


async def test_the_eof_sentinel_starts_the_flush_before_join(
    tmp_path: Path,
) -> None:
    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)

    await transformer.start()
    for entry in _text_block("blk-1", "结论"):
        await transformer.append(entry)  # type: ignore[arg-type]
    await transformer.append(None, last=True)
    flush = transformer._flush
    await transformer.join()

    assert flush is not None
    assert transformer._flush is flush


async def test_a_host_timeout_does_not_cancel_the_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``join`` is shielded, so the worker still drains after the host stops
    waiting; otherwise the events it holds would die with the Chat."""

    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)
    await transformer.start()
    pipeline = transformer._pipeline
    assert pipeline is not None
    drain = pipeline.aclose

    async def slow_drain() -> None:
        await asyncio.sleep(0.05)
        await drain()

    monkeypatch.setattr(pipeline, "aclose", slow_drain)
    for entry in _text_block("blk-1", "结论"):
        await transformer.append(entry)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(transformer.join(), timeout=0.01)
    flush = transformer._flush
    assert flush is not None
    await flush

    assert not flush.cancelled()
    assert len(envelope.biz_events) == 1


async def test_append_and_join_are_no_ops_when_start_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_kwargs: Any) -> None:
        raise RuntimeError("no pipeline for you")

    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.biztrace.transformer.build_pipeline", explode
    )
    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)

    await _drive(transformer, _text_block("blk-1", "结论"))

    assert envelope.biz_events == []


async def test_the_feature_switch_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_BIZ_TRACE_ENABLED", "false")
    envelope = FakeEnvelope()
    transformer = _transformer(tmp_path, envelope)

    await _drive(transformer, _text_block("blk-1", "结论"))

    assert transformer._pipeline is None
    assert envelope.biz_events == []
