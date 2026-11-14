# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging

from qwenpaw_data.tracing import (
    copy_cache_attributes,
    reset_warn_once,
    stamp_baggage,
    warn_once,
)
from qwenpaw_data.tracing.export.spans import (
    BaggageAttributeProcessor,
    DebugSpanProcessor,
    enrich_observation,
)
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider


def test_enrich_observation_maps_chat_and_embedding() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(
        "chat",
        attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "q"},
    ) as span:
        enrich_observation(span)
        assert span.attributes["langfuse.observation.type"] == "generation"
        assert span.attributes["langfuse.observation.model.name"] == "q"
    with tracer.start_as_current_span(
        "emb", attributes={"gen_ai.operation.name": "embedding"}
    ) as span:
        enrich_observation(span)
        assert span.attributes["langfuse.observation.type"] == "embedding"


def test_processor_extra_failure_does_not_raise(caplog) -> None:
    class _Boom(BaggageAttributeProcessor):
        def on_start_extra(self, span, parent_context=None) -> None:
            raise RuntimeError("boom")

    reset_warn_once()
    provider = TracerProvider()
    provider.add_span_processor(_Boom())
    tracer = provider.get_tracer("test")
    with caplog.at_level(logging.WARNING, logger="qwenpaw_data.tracing.export.spans"):
        with tracer.start_as_current_span("chat"):
            pass
    assert "on_start stamping failed" in caplog.text


def test_debug_processor_logs_span_name(caplog) -> None:
    provider = TracerProvider()
    provider.add_span_processor(DebugSpanProcessor())
    tracer = provider.get_tracer("test")
    with caplog.at_level(logging.INFO):
        with tracer.start_as_current_span("followup"):
            pass
    assert "tracing debug span=followup" in caplog.text


def test_warn_once_warns_then_debugs(caplog) -> None:
    reset_warn_once()
    log = logging.getLogger("qwenpaw_data.tracing.tests.warn")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        warn_once(log, "k", "first")
        warn_once(log, "k", "again")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "first"
    assert any(r.getMessage() == "again" for r in debugs)


def test_stamp_baggage_keeps_the_whole_metadata_json() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    metadata = json.dumps({"datasource_id": "ds-1", "dataset_names": ["orders"]})
    ctx = baggage.set_baggage("langfuse.trace.metadata", metadata)
    ctx = baggage.set_baggage("langfuse.trace.metadata.chat_id", "chat-1", context=ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span("chat") as span:
            stamp_baggage(span)
            assert json.loads(span.attributes["langfuse.trace.metadata"]) == {
                "datasource_id": "ds-1",
                "dataset_names": ["orders"],
            }
            assert "langfuse.trace.metadata.chat_id" not in span.attributes
    finally:
        context.detach(token)


def test_stamp_baggage_decodes_tags_json() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    ctx = baggage.set_baggage("langfuse.trace.tags", '["experiment_a"]')
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span("chat") as span:
            stamp_baggage(span)
            assert list(span.attributes["langfuse.trace.tags"]) == ["experiment_a"]
    finally:
        context.detach(token)


def test_copy_cache_attributes_skips_zero() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(
        "chat",
        attributes={
            "agentscope.usage.cache_input_tokens": 3,
            "agentscope.usage.cache_creation_input_tokens": 0,
        },
    ) as span:
        copy_cache_attributes(span)
        assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 3
        assert "gen_ai.usage.cache_creation.input_tokens" not in span.attributes
