# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.tracing import Settings, basic_auth_headers
from qwenpaw_data.tracing.export.install import (
    TracingConfig,
    otlp_traces_endpoint,
    resolve_provider,
    scores_endpoint,
    tracer_install,
    tracer_reset,
    tracer_shutdown,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import ProxyTracerProvider


def test_endpoints_strip_trailing_slash() -> None:
    assert (
        otlp_traces_endpoint("https://lf.example/")
        == "https://lf.example/api/public/otel/v1/traces"
    )
    assert (
        scores_endpoint("https://lf.example/") == "https://lf.example/api/public/scores"
    )


def test_install_skips_when_unconfigured() -> None:
    tracer_reset()
    assert tracer_install(TracingConfig(service_name="t", endpoint=None)) is False


def test_install_debug_only_is_idempotent(monkeypatch) -> None:
    tracer_reset()
    provider = TracerProvider()
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    config = TracingConfig(service_name="t", debug=True)
    assert tracer_install(config) is True
    assert tracer_install(config) is False
    tracer_shutdown()
    tracer_reset()


def test_only_langfuse_gets_the_ingestion_header() -> None:
    langfuse = basic_auth_headers("langfuse", "pk", "sk")
    assert langfuse["x-langfuse-ingestion-version"] == "4"
    assert "x-langfuse-ingestion-version" not in basic_auth_headers(
        "custom", "pk", "sk"
    )
    assert "x-langfuse-ingestion-version" not in basic_auth_headers("other", "pk", "sk")
    assert "x-langfuse-ingestion-version" not in basic_auth_headers("", "pk", "sk")


def test_resolve_provider_reuses_sdk_provider() -> None:
    existing = TracerProvider()
    resolved, created = resolve_provider(
        existing, "qwenpaw-data-context", source_release_version="v1"
    )
    assert resolved is existing
    assert created is False
    assert "langfuse.release" not in existing.resource.attributes
    created_provider, created = resolve_provider(
        ProxyTracerProvider(),
        "qwenpaw-data-context",
        source_release_version="v1",
    )
    assert created is True
    assert created_provider.resource.attributes["service.name"] == "qwenpaw-data-context"
    assert created_provider.resource.attributes["langfuse.release"] == "v1"


def test_resolve_provider_sets_release() -> None:
    provider, created = resolve_provider(
        ProxyTracerProvider(), "qwenpaw-data-host", source_release_version="v1"
    )
    assert created is True
    assert provider.resource.attributes["langfuse.release"] == "v1"


def test_resolve_provider_release_defaults_unknown() -> None:
    provider, created = resolve_provider(ProxyTracerProvider(), "qwenpaw-data-host")
    assert created is True
    assert provider.resource.attributes["langfuse.release"] == "unknown"


def test_settings_from_env_and_to_config(monkeypatch) -> None:
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("TRACING_DEBUG", "true")
    monkeypatch.setenv("TRACING_BACKEND", "langfuse")
    monkeypatch.setenv("TRACING_HOST", "https://langfuse.example/")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")
    monkeypatch.setenv("SOURCE_RELEASE_VERSION", "v1")
    monkeypatch.delenv("TRACING_SERVICE_NAME", raising=False)
    settings = Settings.from_env()
    assert settings.configured()
    assert settings.service_name == ""
    assert settings.source_release_version == "v1"
    config = settings.to_config(service_name="qwenpaw-data-host")
    assert config.service_name == "qwenpaw-data-host"
    assert config.endpoint == "https://langfuse.example/api/public/otel/v1/traces"
    assert config.headers["x-langfuse-ingestion-version"] == "4"
    assert config.debug is True
    assert config.source_release_version == "v1"


def test_settings_source_release_version_unknown_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("SOURCE_RELEASE_VERSION", raising=False)
    assert Settings.from_env().source_release_version == "unknown"
    monkeypatch.setenv("SOURCE_RELEASE_VERSION", "  ")
    assert Settings.from_env().source_release_version == "unknown"


def test_settings_other_backend_omits_v4_marker(monkeypatch) -> None:
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("TRACING_BACKEND", "custom")
    monkeypatch.setenv("TRACING_HOST", "https://backend.example/")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")
    config = Settings.from_env().to_config(service_name="qwenpaw-data-host")
    assert "x-langfuse-ingestion-version" not in config.headers
    assert config.endpoint == "https://backend.example/api/public/otel/v1/traces"


def test_settings_unconfigured_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    monkeypatch.setenv("TRACING_HOST", "https://lf.example")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")
    settings = Settings.from_env()
    assert not settings.configured()
    assert settings.to_config(service_name="t").endpoint is None
