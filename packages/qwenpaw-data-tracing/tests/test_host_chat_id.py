# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.tracing.runtime.host.chat_id import (
    ChatTraceIdGenerator,
    bind_chat_trace,
    reset_chat_trace,
)
from opentelemetry.sdk.trace import TracerProvider


def test_root_span_uses_the_chat_trace_id() -> None:
    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    tracer = provider.get_tracer("test")
    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    token = bind_chat_trace(chat_id)
    try:
        with tracer.start_as_current_span("invoke_agent") as span:
            assert format(span.get_span_context().trace_id, "032x") == chat_id[5:]
    finally:
        reset_chat_trace(token)


def test_hyphenated_chat_id_does_not_set_the_trace_id() -> None:
    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    tracer = provider.get_tracer("test")
    token = bind_chat_trace("chat-f33fa5fa-9c8b-4cb2-8ae4-055f394f3ee5")
    try:
        with tracer.start_as_current_span("invoke_agent") as span:
            assert (
                format(span.get_span_context().trace_id, "032x")
                != "f33fa5fa9c8b4cb28ae4055f394f3ee5"
            )
    finally:
        reset_chat_trace(token)
