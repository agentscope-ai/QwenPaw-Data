# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer
from qwenpaw_data.tracing.runtime.host.generation import attach_generation_span
from opentelemetry.trace import StatusCode


class _Model:
    model = "qwen-test"

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        return SimpleNamespace(
            content={"ok": True},
            usage=SimpleNamespace(input_tokens=2, output_tokens=4),
        )


def test_attach_generation_span_records_output(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    finished: list[str] = []
    model = attach_generation_span(
        _Model(),
        default_name="followup",
        tracer_cls=BaseTracer,
        on_finish=lambda gen, name, resp: finished.append(name),
    )
    asyncio.run(model.generate_structured_output(["hello"], {"type": "object"}))
    span = exporter.get_finished_spans()[0]
    assert span.name == "followup"
    assert span.attributes["gen_ai.request.model"] == "qwen-test"
    assert span.attributes["gen_ai.usage.input_tokens"] == 2
    assert finished == ["followup"]


def test_attach_generation_span_uses_source_step(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    model = attach_generation_span(
        _Model(), default_name="followup", tracer_cls=BaseTracer
    )
    asyncio.run(
        model.generate_structured_output([], {}, source_step="settlement.detect")
    )
    assert exporter.get_finished_spans()[0].name == "settlement.detect"


def test_attach_generation_span_skips_inside_chat(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    model = attach_generation_span(
        _Model(), default_name="followup", tracer_cls=BaseTracer
    )
    with provider.get_tracer("test").start_as_current_span(
        "chat", attributes={"gen_ai.operation.name": "chat"}
    ):
        asyncio.run(model.generate_structured_output(["hi"], {}))
    names = [s.name for s in exporter.get_finished_spans()]
    assert "followup" not in names


def test_attach_generation_span_marks_errors(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Fail(_Model):
        async def generate_structured_output(
            self, messages, structured_model, **kwargs
        ):
            raise RuntimeError("schema rejected")

    model = attach_generation_span(
        _Fail(), default_name="settlement", tracer_cls=BaseTracer
    )
    with pytest.raises(RuntimeError, match="schema rejected"):
        asyncio.run(model.generate_structured_output([], {}))
    span = exporter.get_finished_spans()[0]
    assert span.name == "settlement"
    assert span.status.status_code == StatusCode.ERROR
