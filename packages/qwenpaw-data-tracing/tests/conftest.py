# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def in_memory_tracer(monkeypatch: pytest.MonkeyPatch):
    """Route get_tracer to a fresh in-memory provider."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda *_a, **_k: provider.get_tracer("test"),
    )
    return provider, exporter
