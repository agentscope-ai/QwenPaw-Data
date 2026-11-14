# -*- coding: utf-8 -*-
"""Install one OpenTelemetry exporter aimed at Langfuse v4."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from qwenpaw_data.tracing.export.spans import (
    BaggageAttributeProcessor,
    DebugSpanProcessor,
)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


def basic_auth_headers(
    backend: str, public_key: str, secret_key: str
) -> dict[str, str]:
    """OTLP auth headers; Langfuse v4 ingestion gets its version marker."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if backend.strip().lower() == "langfuse":
        headers["x-langfuse-ingestion-version"] = "4"
    return headers


def otlp_traces_endpoint(host: str) -> str:
    return f"{host.strip().rstrip('/')}/api/public/otel/v1/traces"


def scores_endpoint(host: str) -> str:
    return f"{host.strip().rstrip('/')}/api/public/scores"


@dataclass(frozen=True)
class TracingConfig:
    """Install input: endpoint, headers, processor. Does not read env."""

    service_name: str
    endpoint: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    debug: bool = False
    id_generator: Any = None
    head_processor: SpanProcessor | None = None
    source_release_version: str = "unknown"


def resolve_provider(
    current: object,
    service_name: str,
    *,
    id_generator: Any = None,
    source_release_version: str = "unknown",
) -> tuple[TracerProvider, bool]:
    """Reuse an SDK provider. A proxy provider does not count."""
    if isinstance(current, TracerProvider):
        return current, False
    name = service_name.strip() or "qwenpaw-data"
    resource = Resource.create(
        {
            "service.name": name,
            "langfuse.release": source_release_version,
        }
    )
    return (
        TracerProvider(resource=resource, id_generator=id_generator),
        True,
    )


_installed = False
_closed = False
_provider: TracerProvider | None = None
_owns_provider = False


def tracer_install(config: TracingConfig) -> bool:
    """Register a tracer provider when tracing is configured.

    Returns True when this call performed the install. Idempotent: a second
    call in the same process is a no-op.
    """
    global _installed, _provider, _owns_provider
    if config.endpoint is None and not config.debug:
        return False
    if _installed:
        return False
    provider, created = resolve_provider(
        trace.get_tracer_provider(),
        config.service_name,
        id_generator=config.id_generator,
        source_release_version=config.source_release_version,
    )
    provider.add_span_processor(config.head_processor or BaggageAttributeProcessor())
    if config.debug:
        provider.add_span_processor(DebugSpanProcessor())
    if config.endpoint is not None:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=config.endpoint,
                    headers=config.headers,
                )
            )
        )
    if created:
        trace.set_tracer_provider(provider)
    _provider = provider
    _owns_provider = created
    _installed = True
    logger.info("tracing exporter installed service=%s", config.service_name)
    return True


def tracer_shutdown(timeout_millis: int = 5000) -> None:
    """Flush queued spans; shut the provider down only if we created it."""
    global _closed
    provider = _provider
    if provider is None or _closed:
        return
    _closed = True
    try:
        provider.force_flush(timeout_millis=timeout_millis)
    finally:
        if _owns_provider:
            provider.shutdown()


def tracer_reset() -> None:
    """Test hook. Does not uninstall a process-global provider."""
    global _installed, _closed, _provider, _owns_provider
    _installed = False
    _closed = False
    _provider = None
    _owns_provider = False
