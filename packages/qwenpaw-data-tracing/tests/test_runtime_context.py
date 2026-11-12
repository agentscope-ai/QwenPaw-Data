# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from qwenpaw_data.tracing.core.context.labels import TAGS_KEY, merge_tags
from qwenpaw_data.tracing.runtime.common.propagation import (
    apply_trace_labels,
    attach_incoming_trace,
    attach_remote_parent,
    bind_trace_labels,
    derived_trace_labels,
    detach_incoming_trace,
    reset_trace_labels,
)
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider


def test_derived_trace_labels_omit_empty_metadata() -> None:
    labels = derived_trace_labels(
        user_id="u1",
        session_id="ses-1",
        trace_name="biztrace",
    )
    assert labels["langfuse.trace.name"] == "biztrace"
    assert labels["session.id"] == "ses-1"
    assert "langfuse.trace.metadata" not in labels


def test_remote_parent_continues_the_saved_trace() -> None:
    trace_id = "ab" * 16
    span_id = "cd" * 8
    token = attach_remote_parent(trace_id, span_id)
    assert token is not None
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    try:
        with tracer.start_as_current_span("child") as span:
            current = span.get_span_context()
            assert format(current.trace_id, "032x") == trace_id
            assert span.parent is not None
            assert format(span.parent.span_id, "016x") == span_id
    finally:
        context.detach(token)
    assert attach_remote_parent("zz", span_id) is None


def test_apply_trace_labels_overwrites_conversation_id() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    token = bind_trace_labels(
        {
            "session.id": "ses-parent",
            "gen_ai.conversation.id": "ses-parent",
        }
    )
    try:
        with tracer.start_as_current_span(
            "chat", attributes={"gen_ai.conversation.id": "agentscope"}
        ) as span:
            apply_trace_labels(span)
            assert span.attributes["gen_ai.conversation.id"] == "ses-parent"
            assert span.attributes["session.id"] == "ses-parent"
    finally:
        reset_trace_labels(token)


def test_apply_trace_labels_stamps_tags_as_array() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    token = bind_trace_labels({TAGS_KEY: '["experiment_a"]'})
    try:
        with tracer.start_as_current_span("chat") as span:
            apply_trace_labels(span)
            assert list(span.attributes[TAGS_KEY]) == ["experiment_a"]
    finally:
        reset_trace_labels(token)


def test_merge_tags_rejects_empty_and_overlong() -> None:
    assert merge_tags(["a", "a", "b"]) == ["a", "b"]
    with pytest.raises(ValueError, match="empty"):
        merge_tags([""])
    with pytest.raises(ValueError, match="200"):
        merge_tags(["x" * 201])
    with pytest.raises(TypeError, match="string"):
        merge_tags([1])  # type: ignore[list-item]


def test_attach_incoming_trace_sets_session_baggage() -> None:
    trace_id = "ab" * 16
    span_id = "cd" * 8
    token = attach_incoming_trace(
        {
            "traceparent": f"00-{trace_id}-{span_id}-01",
            "x-cm-host-session-id": "ses-1",
        },
        session_header="x-cm-host-session-id",
        session_baggage_keys=("session.id", "langfuse.session.id"),
    )
    try:
        assert baggage.get_baggage("session.id") == "ses-1"
        assert baggage.get_baggage("langfuse.session.id") == "ses-1"
    finally:
        detach_incoming_trace(token)
