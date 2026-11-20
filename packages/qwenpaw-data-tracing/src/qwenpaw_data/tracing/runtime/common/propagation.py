# -*- coding: utf-8 -*-
"""Trace identity helpers shared by host and CM."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.tracing.core.context.labels import (
    apply_trace_labels,
    bind_trace_labels,
    reset_trace_labels,
)
from opentelemetry import context

__all__ = [
    "apply_trace_labels",
    "attach_incoming_trace",
    "attach_remote_parent",
    "bind_trace_labels",
    "derived_trace_labels",
    "detach_incoming_trace",
    "reset_trace_labels",
]


def derived_trace_labels(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    metadata_json: str | None = None,
) -> dict[str, str]:
    """Labels for a derived trace: same session, caller's metadata as-is."""
    labels = {
        "user.id": user_id,
        "langfuse.user.id": user_id,
        "session.id": session_id,
        "langfuse.session.id": session_id,
        "gen_ai.conversation.id": session_id,
        "langfuse.trace.name": trace_name,
    }
    if metadata_json:
        labels["langfuse.trace.metadata"] = metadata_json
    return labels


def attach_remote_parent(trace_id: str, span_id: str) -> object | None:
    """Make the next span a child of a span that already ended."""
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    try:
        span_context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except ValueError:
        return None
    if not span_context.is_valid:
        return None
    parent = NonRecordingSpan(span_context)
    return context.attach(trace.set_span_in_context(parent))


def attach_incoming_trace(
    headers: Any,
    *,
    session_header: str = "",
    session_baggage_keys: tuple[str, ...] = (),
) -> object | None:
    """Request entry: W3C traceparent extract + session baggage attach."""
    from opentelemetry import baggage
    from opentelemetry.propagate import extract

    ctx = extract(headers)
    session_id = (
        str(headers.get(session_header) or "").strip() if session_header else ""
    )
    if session_id and not baggage.get_baggage("session.id", context=ctx):
        for key in session_baggage_keys:
            ctx = baggage.set_baggage(key, session_id, context=ctx)
    return context.attach(ctx)


def detach_incoming_trace(token: object) -> None:
    """Undo the context ``attach_incoming_trace`` returned a token for."""
    context.detach(token)  # type: ignore[arg-type]
