# -*- coding: utf-8 -*-
"""One tracing entry: AgentScope spans, tool state, chat labels, CM parent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncGenerator, Awaitable, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.middleware import TracingMiddleware as AgentScopeTracingMiddleware
from qwenpaw_data.host.core.tracing.adapters import (
    _inject_cm_traceparent,
    tracer,
)
from qwenpaw_data.host.core.tracing.utils import FAILED_TOOL_STATES
from qwenpaw_data.tracing.core.names import Score
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer
from qwenpaw_data.tracing.export.score import tracer_send_span_score

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext

logger = logging.getLogger(__name__)


_TOOL_STATES = frozenset({"success", "error", "interrupted", "denied"})
# denied is a user decision, not a tool failure: it gets no score.
_TOOL_FAILED_VALUES = {"success": 0.0, "error": 1.0, "interrupted": 1.0}


async def stamp_tool_state(item: object) -> None:
    """Write the tool result state while the execute_tool span is still open."""

    state = getattr(item, "state", None)
    text = getattr(state, "value", None)
    if not isinstance(text, str):
        text = str(state)
    text = text.lower()
    if text not in _TOOL_STATES:
        return
    BaseTracer.stamp(
        metadata={"tool_state": text},
        failed=text in FAILED_TOOL_STATES,
        error=text,
    )
    value = _TOOL_FAILED_VALUES.get(text)
    if value is not None:
        await tracer_send_span_score(Score.TOOL_FAILED, value)


class TracingMiddleware(MiddlewareBase):
    def __init__(self, *, run_context: RunContext | None = None) -> None:
        self._tracing = AgentScopeTracingMiddleware()
        self._run_context = run_context
        self._tracer = tracer(run_context) if run_context else None
        self._cm_injected: dict[int, tuple[str, str | None]] = {}

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async def bound(**kwargs: object) -> AsyncGenerator:
            if self._tracer is None:
                async for item in next_handler(**kwargs):
                    yield item
                return
            stream = next_handler(**kwargs)
            with self._tracer.set():
                await self._tracer.set_span()
                if self._run_context is not None:
                    await _inject_cm_traceparent(self._run_context, self._cm_injected)
            while True:
                try:
                    with self._tracer.set():
                        item = await anext(stream)
                except StopAsyncIteration:
                    return
                yield item

        async for item in self._tracing.on_reply(agent, input_kwargs, bound):
            yield item

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async def stamped(**kwargs: object) -> AsyncGenerator:
            if self._run_context is not None:
                await _inject_cm_traceparent(self._run_context, self._cm_injected)
            async for item in next_handler(**kwargs):
                await stamp_tool_state(item)
                yield item

        acting = getattr(self._tracing, "on_acting", None)
        if acting is None:
            async for item in stamped(**input_kwargs):
                yield item
            return
        async for item in acting(agent, input_kwargs, stamped):
            yield item

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[object]],
    ) -> object:
        return await self._tracing.on_model_call(agent, input_kwargs, next_handler)
