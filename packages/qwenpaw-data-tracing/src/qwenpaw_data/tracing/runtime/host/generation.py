# -*- coding: utf-8 -*-
"""Auto-span wrapper for ``generate_structured_output`` model calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qwenpaw_data.tracing.core.trace.handles import _Generation
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer
from opentelemetry import trace


def _current_span_is_chat() -> bool:
    attributes = getattr(trace.get_current_span(), "attributes", None) or {}
    return attributes.get("gen_ai.operation.name") == "chat"


def attach_generation_span(
    model: Any,
    *,
    default_name: str,
    tracer_cls: type[BaseTracer],
    on_finish: Callable[[_Generation, str, Any], None] | None = None,
) -> Any:
    """Span ``generate_structured_output`` unless a chat span is current."""

    original = model.generate_structured_output
    model_name = str(getattr(model, "model", "") or "")

    async def traced(
        messages: Any,
        structured_model: Any,
        *,
        source_step: Any = None,
        **kwargs: Any,
    ) -> Any:
        if _current_span_is_chat():
            return await original(messages, structured_model, **kwargs)
        name = source_step or default_name
        with tracer_cls.generation(
            name,
            model=model_name,
            input_payload=messages,
        ) as gen:
            response = await original(messages, structured_model, **kwargs)
            gen.finish(response)
            if on_finish is not None:
                on_finish(gen, name, response)
            return response

    model.generate_structured_output = traced  # type: ignore[method-assign]
    return model
