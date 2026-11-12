# -*- coding: utf-8 -*-
"""Span handles returned by the tracer entry points."""

from __future__ import annotations

import logging
from typing import Any

from qwenpaw_data.tracing.core.utils.serialize import dump_json
from qwenpaw_data.tracing.core.utils.warnonce import warn_once
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)


def _read_field(obj: Any, *names: str) -> Any:
    """Read the first non-None field, dict or object, pydantic extras included."""
    for name in names:
        if isinstance(obj, dict) and obj.get(name) is not None:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for name in names:
            if extra.get(name) is not None:
                return extra[name]
    return None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class _Generation:
    """Handle returned by ``BaseTracer.generation``; finish writes output."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        """Write one extra attribute onto the open generation span."""
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            logger.debug("generation attribute failed", exc_info=True)

    def metadata(self, key: str, value: Any) -> None:
        """Write one business metadata key; the platform prefix lives here."""
        self.set_attribute(f"langfuse.observation.metadata.{key}", value)

    def finish(self, response: Any, *, span_name: str | None = None) -> None:
        """Write output messages and usage/cache attributes."""
        if self._span is None:
            return
        span = self._span
        try:
            content = getattr(response, "content", None)
            text = dump_json(content if content is not None else response)
            span.set_attribute("gen_ai.output.messages", text)
            span.set_attribute("langfuse.observation.output", text)
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            input_tokens = getattr(usage, "input_tokens", None)
            if input_tokens is None:
                input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            if output_tokens is None:
                output_tokens = getattr(usage, "completion_tokens", None)
            if input_tokens is not None:
                span.set_attribute(
                    "gen_ai.usage.input_tokens", _non_negative_int(input_tokens)
                )
            if output_tokens is not None:
                span.set_attribute(
                    "gen_ai.usage.output_tokens", _non_negative_int(output_tokens)
                )
            details = _read_field(
                usage, "prompt_tokens_details", "input_tokens_details"
            )
            cache_read = _read_field(usage, "cache_read_tokens", "cache_input_tokens")
            if cache_read is None:
                cache_read = _read_field(details, "cached_tokens")
            if cache_read:
                span.set_attribute(
                    "gen_ai.usage.cache_read.input_tokens",
                    _non_negative_int(cache_read),
                )
            cache_creation = _read_field(details, "cache_creation_input_tokens")
            if cache_creation is None:
                cache_creation = _read_field(usage, "cache_creation_input_tokens")
            if cache_creation:
                span.set_attribute(
                    "gen_ai.usage.cache_creation.input_tokens",
                    _non_negative_int(cache_creation),
                )
        except Exception:
            warn_once(
                logger, "generation_finish", "generation finish failed", exc_info=True
            )


class _RootSpan:
    """Manual-lifecycle root span. Returned by ``BaseTracer.start``."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def activate(self) -> object | None:
        """Attach this span in an empty context (no baggage leak). Token out."""
        if self._span is None:
            return None
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        return otel_context.attach(
            trace.set_span_in_context(self._span, otel_context.Context())
        )

    def deactivate(self, token: object | None) -> None:
        if token is None:
            return
        from opentelemetry import context as otel_context

        try:
            otel_context.detach(token)  # type: ignore[arg-type]
        except Exception:
            logger.debug("root span deactivate failed", exc_info=True)

    def end(self, error: BaseException | str | None = None) -> None:
        """Close the root span; swallow errors (tracing never breaks)."""
        span = self._span
        self._span = None
        if span is None:
            return
        try:
            if error is not None:
                text = str(error)
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_status(StatusCode.ERROR, text)
            span.end()
        except Exception:
            logger.debug("root span end failed", exc_info=True)


__all__ = ["_Generation", "_RootSpan"]
