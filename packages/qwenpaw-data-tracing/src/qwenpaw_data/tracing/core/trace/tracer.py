# -*- coding: utf-8 -*-
"""Class-method tracing entry points; one-line calls, no-op when tracing is off."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

from qwenpaw_data.tracing.core.trace.handles import _Generation, _RootSpan
from qwenpaw_data.tracing.core.utils.serialize import dump_json
from qwenpaw_data.tracing.core.utils.warnonce import warn_once
from opentelemetry import trace
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)


def _step_name(step: object) -> str:
    """Span name from a step: StrEnum member or plain string."""
    value = getattr(step, "value", None)
    if isinstance(value, str) and value:
        return value
    return str(step)


def _merge(
    attributes: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    input: Any,
) -> dict[str, Any]:
    """Business values in; the ``langfuse.observation.*`` prefixes are added only here."""
    attrs = dict(attributes or {})
    for key, value in (metadata or {}).items():
        attrs[f"langfuse.observation.metadata.{key}"] = value
    if input is not None:
        attrs["langfuse.observation.input"] = dump_json(input)
    return attrs


class BaseTracer:
    """Class-method tracing entry points; subclass per service or domain."""

    TRACER_NAME: ClassVar[str] = "qwenpaw-data"

    @classmethod
    def _tracer(cls) -> Any:
        return trace.get_tracer(cls.TRACER_NAME)

    @classmethod
    def log(
        cls,
        step: object,
        *,
        attributes: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        input: Any = None,
        error: str | BaseException | None = None,
    ) -> None:
        """One-shot span: open, write attributes, close immediately."""
        attrs = _merge(attributes, metadata, input)
        tracer = cls._tracer()
        with tracer.start_as_current_span(_step_name(step)) as span:
            for key, value in attrs.items():
                span.set_attribute(key, value)
            if error:
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_status(StatusCode.ERROR, str(error))
            else:
                span.set_attribute("langfuse.observation.level", "DEFAULT")
                span.set_status(StatusCode.OK)

    @classmethod
    @contextmanager
    def span(
        cls,
        step: object,
        *,
        attributes: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        input: Any = None,
        error: str | BaseException | None = None,
    ) -> Iterator[Any]:
        """Time one block. Exceptions: record + ERROR status, then re-raise."""
        attrs = _merge(attributes, metadata, input)
        tracer = cls._tracer()
        with tracer.start_as_current_span(_step_name(step)) as span:
            for key, value in attrs.items():
                span.set_attribute(key, value)
            if error:
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_status(StatusCode.ERROR, str(error))
            try:
                yield span
            except BaseException as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @classmethod
    @contextmanager
    def generation(
        cls,
        step: object,
        *,
        model: str = "",
        input_payload: Any = None,
        model_type: str = "chat",
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_Generation]:
        """Wrap one model call; ``gen.finish(response)`` writes output/usage."""
        embedding = model_type == "embedding"
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "embeddings" if embedding else "chat",
            "langfuse.observation.type": "embedding" if embedding else "generation",
            "gen_ai.input.messages": dump_json(input_payload),
            "langfuse.observation.input": dump_json(input_payload),
        }
        if model:
            attrs["gen_ai.request.model"] = model
            attrs["langfuse.observation.model.name"] = model
        for key, value in (attributes or {}).items():
            if isinstance(value, (str, bool, int, float)):
                attrs[key] = value
        tracer = cls._tracer()
        with tracer.start_as_current_span(_step_name(step), attributes=attrs) as span:
            gen = _Generation(span)
            try:
                yield gen
            except BaseException as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @classmethod
    def stamp(
        cls,
        *,
        attributes: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        failed: bool = False,
        error: str = "",
    ) -> None:
        """Write onto the already-open current span. No-op without a valid current span."""
        span = trace.get_current_span()
        if not span.get_span_context().is_valid:
            return
        try:
            for key, value in _merge(attributes, metadata, None).items():
                span.set_attribute(key, value)
            if failed:
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_status(StatusCode.ERROR, error or "failed")
            else:
                span.set_attribute("langfuse.observation.level", "DEFAULT")
                span.set_status(StatusCode.OK)
        except Exception:
            warn_once(logger, "stamp", "trace stamp failed", exc_info=True)

    @classmethod
    def start(
        cls, step: object, *, attributes: dict[str, Any] | None = None
    ) -> _RootSpan:
        """Escape hatch: manual-lifecycle root span, returned as a ``_RootSpan`` handle."""
        tracer = cls._tracer()
        from opentelemetry import context as otel_context

        span = tracer.start_span(
            _step_name(step),
            attributes=attributes or {},
            context=otel_context.Context(),
        )
        return _RootSpan(span)
