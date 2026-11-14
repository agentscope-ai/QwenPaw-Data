# -*- coding: utf-8 -*-
"""OSS defaults: no export unless configured; credentials and content stay out of logs."""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from qwenpaw_data.tracing import Settings
from qwenpaw_data.tracing.export import install
from qwenpaw_data.tracing.export.install import tracer_install, tracer_reset
from qwenpaw_data.tracing.export.spans import DebugSpanProcessor
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

_EXPORT_ENV = {
    "TRACING_ENABLED": "1",
    "TRACING_HOST": "https://lf.example",
    "TRACING_PUBLIC_KEY": "pk-test-public",
    "TRACING_SECRET_KEY": "sk-test-secret",
}

_NOOP_SCRIPT = """
import asyncio
import importlib
import io
import pkgutil
from contextlib import ExitStack
from unittest.mock import patch
from opentelemetry import trace

real_open = io.open


def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        raise AssertionError(f"opened {file!r} for writing")
    return real_open(file, mode, *args, **kwargs)


provider = trace.get_tracer_provider()
with ExitStack() as stack:
    guards = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
              for target in (
                  "socket.socket.connect",
                  "socket.getaddrinfo",
                  "threading.Thread.start",
                  "httpx.AsyncClient",
                  "opentelemetry.trace.set_tracer_provider",
                  "opentelemetry.sdk.trace.TracerProvider",
                  "opentelemetry.sdk.trace.export.BatchSpanProcessor",
                  "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
              )]
    stack.enter_context(patch("builtins.open", guarded_open))
    stack.enter_context(patch("io.open", guarded_open))
    import qwenpaw_data.tracing as tracing
    for module in pkgutil.walk_packages(tracing.__path__, tracing.__name__ + "."):
        importlib.import_module(module.name)
    from qwenpaw_data.tracing.core.names import Step, Tag
    from qwenpaw_data.tracing.export.score import tracer_send_score, tracer_delete_score
    from qwenpaw_data.tracing.runtime.host.chat import tracer

    settings = tracing.Settings.from_env()
    assert not settings.configured()
    assert not tracing.tracer_install(settings.to_config(service_name="test"))
    tracing.BaseTracer.log("event", input={"prompt": "private"})
    with tracing.BaseTracer.span("span") as span:
        assert not span.is_recording()
    with tracing.BaseTracer.generation("generation", input_payload=["private"]) as gen:
        gen.finish({"ok": True})
    tracing.BaseTracer.stamp(metadata={"ok": True})
    root = tracing.BaseTracer.start("root")
    token = root.activate()
    root.deactivate(token)
    root.end()
    from qwenpaw_data.tracing.runtime.host.chat import tracer_session

    session = tracer_session(user_id="u", session_id="s")

    async def exercise():
        async with tracer(chat_id="chat-" + "ab" * 16, session=session) as chat:
            with chat.set():
                assert chat.get() is None
        async with tracer(step=Step.SKILL_EVOLUTION, session=session,
                          tags=[Tag.SKILL_EVOLUTION]):
            pass
        await tracer_send_score(name="stop", value=1, chat_id="chat-1")
        await tracer_delete_score(id="stop:chat-1")

    asyncio.run(exercise())
    tracing.tracer_shutdown()
    assert trace.get_tracer_provider() is provider
    assert all(guard.call_count == 0 for guard in guards)
"""


def test_import_and_runtime_are_noop_by_default(tmp_path: Path) -> None:
    """No TRACING_* env: no provider, exporter, thread, socket, HTTP or file."""
    dirs = {name: tmp_path / name for name in ("home", "work", "tmp")}
    for path in dirs.values():
        path.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TRACING_", "OTEL_"))
        and key != "SOURCE_RELEASE_VERSION"
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.update(
        HOME=str(dirs["home"]),
        TMPDIR=str(dirs["tmp"]),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", _NOOP_SCRIPT],
        cwd=dirs["work"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


@pytest.mark.parametrize("missing", sorted(_EXPORT_ENV))
def test_credentials_are_dropped_unless_export_is_configured(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    for name, value in _EXPORT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    settings = Settings.from_env()
    config = settings.to_config(service_name="t")
    assert not settings.configured()
    assert config.endpoint is None
    assert config.headers == {}
    tracer_reset()
    assert tracer_install(config) is False


def test_credentials_only_reach_the_configured_exporter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exporters: list[dict] = []

    class _Exporter:
        def __init__(self, **kwargs) -> None:
            exporters.append(kwargs)

    class _Batch(SpanProcessor):
        def __init__(self, exporter) -> None:
            self.exporter = exporter

    provider = TracerProvider()
    monkeypatch.setattr(install, "OTLPSpanExporter", _Exporter)
    monkeypatch.setattr(install, "BatchSpanProcessor", _Batch)
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    for name, value in _EXPORT_ENV.items():
        monkeypatch.setenv(name, value)
    tracer_reset()
    try:
        with caplog.at_level(logging.DEBUG, logger="qwenpaw_data.tracing"):
            config = Settings.from_env().to_config(service_name="t")
            assert tracer_install(config) is True
    finally:
        tracer_reset()
    token = base64.b64encode(b"pk-test-public:sk-test-secret").decode()
    assert exporters == [
        {
            "endpoint": "https://lf.example/api/public/otel/v1/traces",
            "headers": {
                "Authorization": f"Basic {token}",
                "x-langfuse-ingestion-version": "4",
            },
        }
    ]
    assert "sk-test-secret" not in caplog.text
    assert token not in caplog.text


def test_debug_log_omits_prompt_and_output_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = TracerProvider()
    provider.add_span_processor(DebugSpanProcessor())
    tracer = provider.get_tracer("test")
    attributes = {
        "gen_ai.request.model": "test-model",
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.input.messages": '[{"content": "private prompt"}]',
        "langfuse.observation.input": "private prompt",
        "langfuse.observation.output": "private answer",
    }
    with caplog.at_level(logging.INFO, logger="qwenpaw_data.tracing"):
        with tracer.start_as_current_span("decision", attributes=attributes):
            pass
    assert "gen_ai.request.model=test-model" in caplog.text
    assert "gen_ai.usage.input_tokens=7" in caplog.text
    assert "private" not in caplog.text
