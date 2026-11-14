# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest
from qwenpaw_data.tracing.core.context.labels import TAGS_KEY, apply_trace_labels
from qwenpaw_data.tracing.runtime.host.chat import (
    ChatTracer,
    tracer,
    tracer_end,
    tracer_session,
    tracer_span,
    tracer_start,
)
from qwenpaw_data.tracing.runtime.host.chat_id import (
    ChatTraceIdGenerator,
    bind_chat_trace,
    reset_chat_trace,
)
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


class _LabelProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        apply_trace_labels(span)

    def on_end(self, span: ReadableSpan) -> None:
        return None


def test_chat_tracer_set_stamps_span(in_memory_tracer) -> None:
    provider, _ = in_memory_tracer
    session = tracer_session("u1", "ses-1")
    chat = ChatTracer(session)
    with provider.get_tracer("test").start_as_current_span("reply") as span:
        with chat.set():
            assert span.attributes["langfuse.trace.name"] == "chat"
            assert span.attributes["session.id"] == "ses-1"
            assert TAGS_KEY not in (span.attributes or {})


@pytest.mark.asyncio
async def test_chat_tracer_set_span_once(in_memory_tracer) -> None:
    provider, _ = in_memory_tracer
    saved: list[str] = []

    async def _save(span_id: str) -> None:
        saved.append(span_id)

    chat = ChatTracer(
        tracer_session("u1", "ses-1"),
        save_span_id=_save,
    )
    with provider.get_tracer("test").start_as_current_span("reply"):
        await chat.set_span()
        await chat.set_span()
    assert len(saved) == 1
    assert len(saved[0]) == 16


@pytest.mark.asyncio
async def test_chat_tracer_continues_chat_and_detaches() -> None:
    attached: list[str] = []

    async def _attach():
        attached.append("ok")
        return None

    chat_id = "chat-" + "ab" * 16
    async with tracer(
        tracer_session("u1", "ses-1"),
        chat_id=chat_id,
        attach_saved=_attach,
    ):
        pass
    assert attached == ["ok"]


def test_start_with_step_does_not_reuse_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda *_a, **_k: provider.get_tracer("test"),
    )
    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    parent_id = chat_id[5:]
    token = bind_chat_trace(chat_id)
    try:
        with provider.get_tracer("test").start_as_current_span("chat"):
            started = tracer_start(
                tracer_session("u1", "ses-1"),
                chat_id=chat_id,
                step="settlement",
            )
            assert started.trace_id != parent_id
            assert started.trace_id is not None
            assert started._source_trace_id == parent_id
            with started:
                with tracer_span("tick"):
                    pass
            tracer_end(started)
    finally:
        reset_chat_trace(token)

    root = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    assert root.parent is None
    assert format(root.context.trace_id, "032x") != parent_id
    assert parent_id in (started._labels or {}).get("langfuse.trace.metadata", "")


@pytest.mark.asyncio
async def test_start_with_step_ends_error_status(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    with pytest.raises(RuntimeError, match="no"):
        async with tracer(
            tracer_session("u1", "ses-1"),
            chat_id="chat-" + "ab" * 16,
            step="settlement",
        ):
            raise RuntimeError("no")
    span = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    assert span.status.status_code == StatusCode.ERROR


def test_set_stamps_caller_tags(in_memory_tracer) -> None:
    provider, _ = in_memory_tracer
    session = tracer_session("u1", "ses-1")
    chat = ChatTracer(session, tags=["experiment_a"])
    with (
        provider.get_tracer("test").start_as_current_span("reply") as span,
        chat.set(),
    ):
        assert list(span.attributes[TAGS_KEY]) == ["experiment_a"]


@pytest.mark.asyncio
async def test_chat_tags_reach_middleware_set(in_memory_tracer) -> None:
    provider, _ = in_memory_tracer
    session = tracer_session("u1", "ses-1")
    chat_id = "chat-" + "ab" * 16
    async with tracer(session, chat_id=chat_id, tags=["experiment_a"]):
        middleware = ChatTracer(session)
        with (
            provider.get_tracer("test").start_as_current_span("reply") as span,
            middleware.set(),
        ):
            assert list(span.attributes[TAGS_KEY]) == ["experiment_a"]


def test_tracer_start_tags_on_root_and_child(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    provider.add_span_processor(_LabelProcessor())
    started = tracer_start(
        tracer_session("u1", "ses-1"),
        chat_id="chat-" + "ab" * 16,
        step="settlement",
        tags=["experiment_a", "experiment_a"],
    )
    with started, tracer_span("tick"):
        pass
    tracer_end(started)
    root = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    child = next(s for s in exporter.get_finished_spans() if s.name == "tick")
    assert list(root.attributes[TAGS_KEY]) == ["experiment_a"]
    assert list(child.attributes[TAGS_KEY]) == ["experiment_a"]


def test_tracer_start_omits_tags_without_caller(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    provider.add_span_processor(_LabelProcessor())
    started = tracer_start(
        tracer_session("u1", "ses-1"),
        chat_id="chat-" + "ab" * 16,
        step="settlement",
    )
    with started:
        pass
    tracer_end(started)
    root = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    assert TAGS_KEY not in (root.attributes or {})


@pytest.mark.asyncio
async def test_related_trace_does_not_inherit_chat_tags(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    provider.add_span_processor(_LabelProcessor())
    session = tracer_session("u1", "ses-1")
    chat_id = "chat-" + "ab" * 16
    async with tracer(session, chat_id=chat_id, tags=["experiment_a"]):
        started = tracer_start(session, chat_id=chat_id, step="settlement")
        with started:
            pass
        tracer_end(started)
    root = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    assert TAGS_KEY not in (root.attributes or {})


def test_invalid_tags_fail_at_construction() -> None:
    with pytest.raises(ValueError, match="empty"):
        ChatTracer(tags=[""])
    with pytest.raises(ValueError, match="200"):
        ChatTracer(tags=["x" * 201])


def test_related_root_writes_desc_without_source_trace_id(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    provider.add_span_processor(_LabelProcessor())
    started = tracer_start(
        tracer_session("u1", "ses-1"),
        step="task",
        metadata={"desc": "按支付时间统计 8 月收入总额"},
    )
    with started:
        pass
    tracer_end(started)
    root = next(s for s in exporter.get_finished_spans() if s.name == "task")
    assert root.attributes["session.id"] == "ses-1"
    assert root.attributes["user.id"] == "u1"
    assert json.loads(root.attributes["langfuse.trace.metadata"]) == {
        "desc": "按支付时间统计 8 月收入总额"
    }


def test_tracer_session_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        tracer_session("", "ses-1")
    with pytest.raises(ValueError, match="empty"):
        tracer_session("u1", "  ")


def test_metadata_rejects_reserved_keys_and_unserializable() -> None:
    session = tracer_session("u1", "ses-1")
    with pytest.raises(ValueError, match="source_trace_id"):
        ChatTracer(session, metadata={"source_trace_id": "x"})
    with pytest.raises(ValueError, match="JSON"):
        ChatTracer(session, metadata={"when": object()})
