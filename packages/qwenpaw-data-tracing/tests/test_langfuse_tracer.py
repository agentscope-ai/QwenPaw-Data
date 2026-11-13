# -*- coding: utf-8 -*-
from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace

import pytest
from qwenpaw_data.tracing.core.trace.handles import _Generation
from qwenpaw_data.tracing.core.trace.session import BaseSessionTracer
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer
from qwenpaw_data.tracing.runtime.host.chat import (
    tracer_end,
    tracer_session,
    tracer_start,
)
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import StatusCode


class _Step(StrEnum):
    DECISION = "decision"


def test_base_tracer_log_and_span(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    BaseTracer.log(
        _Step.DECISION,
        metadata={"k": "v"},
        input={"q": 1},
    )
    with pytest.raises(RuntimeError, match="boom"):
        with BaseTracer.span("work", attributes={"a": 1}):
            raise RuntimeError("boom")
    spans = exporter.get_finished_spans()
    logged = next(s for s in spans if s.name == "decision")
    assert logged.attributes["langfuse.observation.metadata.k"] == "v"
    assert logged.status.status_code == StatusCode.OK
    failed = next(s for s in spans if s.name == "work")
    assert failed.status.status_code == StatusCode.ERROR


def test_base_tracer_generation_and_stamp(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    with BaseTracer.generation(
        "decision",
        model="qwen",
        input_payload=[{"role": "user"}],
        model_type="chat",
    ) as gen:
        BaseTracer.stamp(metadata={"ok": True})
        gen.metadata("extra", 1)
        gen.finish(type("R", (), {"content": {"a": 1}, "usage": None})())
    span = exporter.get_finished_spans()[0]
    assert span.attributes["langfuse.observation.type"] == "generation"
    assert span.attributes["langfuse.observation.model.name"] == "qwen"
    assert span.attributes["langfuse.observation.metadata.ok"] is True
    assert span.attributes["langfuse.observation.metadata.extra"] == 1


def test_base_tracer_start_root_is_detached(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    root = BaseTracer.start("settlement")
    token = root.activate()
    try:
        with provider.get_tracer("test").start_as_current_span("child"):
            pass
    finally:
        root.deactivate(token)
        root.end()
    names = [s.name for s in exporter.get_finished_spans()]
    assert "child" in names
    assert "settlement" in names
    child = next(s for s in exporter.get_finished_spans() if s.name == "child")
    assert child.parent is not None


def test_base_tracer_stamp_noop_without_span() -> None:
    BaseTracer.stamp(failed=True, error="x")


def test_session_labels_and_baggage() -> None:
    session = BaseSessionTracer(user_id="u1", session_id="ses-1")
    labels = session.labels(metadata_json='{"k":1}')
    assert labels["langfuse.user.id"] == "u1"
    assert labels["langfuse.session.id"] == "ses-1"
    assert labels["langfuse.trace.metadata"] == '{"k":1}'
    token = session.attach_baggage(labels)
    try:
        assert baggage.get_baggage("session.id") == "ses-1"
        assert baggage.get_baggage("gen_ai.conversation.id") is None
    finally:
        context.detach(token)


def test_start_with_step_is_independent_root(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    started = tracer_start(
        tracer_session("u1", "ses-1"),
        chat_id="chat-" + "ab" * 16,
        step="biztrace",
    )
    with started:
        with provider.get_tracer("test").start_as_current_span("child"):
            pass
    tracer_end(started, "failed")
    root = next(s for s in exporter.get_finished_spans() if s.name == "biztrace")
    assert root.status.status_code == StatusCode.ERROR
    assert root.parent is None


def test_usage_attributes_keep_input_and_write_nonzero_cache() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=2,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=4,
                cache_creation_input_tokens=0,
            ),
        )
    )
    with tracer.start_as_current_span("decision") as span:
        _Generation(span).finish(response)
        assert span.attributes["gen_ai.usage.input_tokens"] == 10
        assert span.attributes["gen_ai.usage.output_tokens"] == 2
        assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 4
        assert "gen_ai.usage.cache_creation.input_tokens" not in span.attributes


def test_finish_generation_records_tokens_and_output() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    response = SimpleNamespace(
        content={"questions": ["next"]},
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
    )
    with tracer.start_as_current_span("followup") as span:
        _Generation(span).finish(response)
        assert span.attributes["gen_ai.usage.input_tokens"] == 3
        assert span.attributes["gen_ai.usage.output_tokens"] == 5
        assert "gen_ai.usage.cache_read.input_tokens" not in span.attributes
        assert "next" in span.attributes["langfuse.observation.output"]


def test_finish_generation_records_nonzero_cache_tokens() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    response = SimpleNamespace(
        content="ok",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_input_tokens=4,
            cache_creation_input_tokens=0,
        ),
    )
    with tracer.start_as_current_span("followup") as span:
        _Generation(span).finish(response)
        assert span.attributes["gen_ai.usage.input_tokens"] == 10
        assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 4
        assert "gen_ai.usage.cache_creation.input_tokens" not in span.attributes
