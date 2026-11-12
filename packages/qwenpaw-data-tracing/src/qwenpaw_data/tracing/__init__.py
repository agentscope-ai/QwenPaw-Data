# -*- coding: utf-8 -*-
"""Shared OpenTelemetry-on-Langfuse plumbing for QwenPawData packages."""

from qwenpaw_data.tracing.core.trace.session import BaseSessionTracer
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer
from qwenpaw_data.tracing.core.utils.serialize import dump_json, plain
from qwenpaw_data.tracing.core.utils.warnonce import reset_warn_once, warn_once
from qwenpaw_data.tracing.export.install import (
    TracingConfig,
    basic_auth_headers,
    otlp_traces_endpoint,
    resolve_provider,
    tracer_install,
    tracer_reset,
    tracer_shutdown,
)
from qwenpaw_data.tracing.export.settings import Settings
from qwenpaw_data.tracing.export.spans import (
    BAGGAGE_KEYS,
    CACHE_ATTRIBUTES,
    METADATA_KEY,
    BaggageAttributeProcessor,
    DebugSpanProcessor,
    copy_cache_attributes,
    enrich_observation,
    stamp_baggage,
)

__all__ = [
    "BAGGAGE_KEYS",
    "CACHE_ATTRIBUTES",
    "METADATA_KEY",
    "BaggageAttributeProcessor",
    "BaseSessionTracer",
    "BaseTracer",
    "DebugSpanProcessor",
    "TracingConfig",
    "Settings",
    "basic_auth_headers",
    "copy_cache_attributes",
    "dump_json",
    "enrich_observation",
    "tracer_install",
    "otlp_traces_endpoint",
    "plain",
    "tracer_reset",
    "reset_warn_once",
    "resolve_provider",
    "tracer_shutdown",
    "stamp_baggage",
    "warn_once",
]
