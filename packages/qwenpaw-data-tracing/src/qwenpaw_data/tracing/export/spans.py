# -*- coding: utf-8 -*-
"""Span processors and attribute helpers."""

from __future__ import annotations

import logging

from qwenpaw_data.tracing.core.context.labels import TAGS_KEY, stamp_tag_attribute
from qwenpaw_data.tracing.core.utils.warnonce import warn_once
from opentelemetry.baggage import get_baggage
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

logger = logging.getLogger(__name__)

BAGGAGE_KEYS = (
    "user.id",
    "langfuse.user.id",
    "session.id",
    "langfuse.session.id",
    "langfuse.trace.name",
    "langfuse.trace.metadata",
    TAGS_KEY,
)
METADATA_KEY = "langfuse.trace.metadata"

CACHE_ATTRIBUTES = (
    ("agentscope.usage.cache_input_tokens", "gen_ai.usage.cache_read.input_tokens"),
    (
        "agentscope.usage.cache_creation_input_tokens",
        "gen_ai.usage.cache_creation.input_tokens",
    ),
)

_DEBUG_KEYS = (
    "gen_ai.request.model",
    "langfuse.observation.type",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    "agentscope.usage.cache_input_tokens",
    "agentscope.usage.cache_creation_input_tokens",
)


def stamp_baggage(span: Span, parent_context: Context | None = None) -> None:
    """Copy trace-wide baggage onto a span so Langfuse v4 filters see it."""
    for key in BAGGAGE_KEYS:
        value = get_baggage(key, context=parent_context)
        if not isinstance(value, str) or not value:
            continue
        if key == TAGS_KEY:
            stamp_tag_attribute(span, value)
        else:
            span.set_attribute(key, value)
    metadata = get_baggage(METADATA_KEY, context=parent_context)
    if isinstance(metadata, str) and metadata:
        span.set_attribute(METADATA_KEY, metadata)
    session_id = get_baggage("session.id", context=parent_context)
    if isinstance(session_id, str) and session_id:
        span.set_attribute("gen_ai.conversation.id", session_id)


def copy_cache_attributes(span: ReadableSpan) -> None:
    """Copy AgentScope cache counts onto the GenAI keys before export."""
    attributes = getattr(span, "_attributes", None)
    if attributes is None:
        return
    store = getattr(attributes, "_dict", None)
    if store is None:
        store = attributes
    for source, dest in CACHE_ATTRIBUTES:
        raw = store.get(source)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if value:
            store[dest] = value


def enrich_observation(span: Span) -> None:
    """Map GenAI operation/model attributes onto Langfuse observation keys."""
    attributes = span.attributes or {}
    operation = attributes.get("gen_ai.operation.name")
    if operation == "chat":
        span.set_attribute("langfuse.observation.type", "generation")
    elif operation in {"embedding", "embeddings"}:
        span.set_attribute("langfuse.observation.type", "embedding")
    model_name = attributes.get("gen_ai.request.model")
    if isinstance(model_name, str) and model_name:
        span.set_attribute("langfuse.observation.model.name", model_name)


class DebugSpanProcessor(SpanProcessor):
    """Log token fields for each finished span. Prompts stay out of the log."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        fields = " ".join(f"{key}={attributes.get(key, '-')}" for key in _DEBUG_KEYS)
        logger.info("tracing debug span=%s %s", span.name, fields)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class BaggageAttributeProcessor(SpanProcessor):
    """Copy trace-wide baggage onto every span; hooks for package extras."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            stamp_baggage(span, parent_context)
            enrich_observation(span)
            self.on_start_extra(span, parent_context)
        except Exception:
            warn_once(
                logger,
                "on_start",
                "tracing on_start stamping failed",
                exc_info=True,
            )

    def on_end(self, span: ReadableSpan) -> None:
        try:
            self.on_end_extra(span)
            copy_cache_attributes(span)
        except Exception:
            warn_once(
                logger,
                "on_end",
                "tracing on_end stamping failed",
                exc_info=True,
            )

    def on_start_extra(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end_extra(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
