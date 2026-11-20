# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

from agentscope.event import (
    ExternalExecutionResultEvent,
    HintBlockEvent,
    ReplyEndEvent,
    RequireExternalExecutionEvent,
)
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase
from agentscope.model import FinishedReason
from agentscope.types import ReplyFinishedReason

from qwenpaw_data.host.core.query_log import Step, log_error, log_info

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from agentscope.model import ChatResponse
    from qwenpaw_data.host.core.runtime.context import RunContext


class QueryLogMiddleware(MiddlewareBase):
    def __init__(self, *, run_context: RunContext) -> None:
        self._ctx = run_context

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        self._log_entry(agent, input_kwargs.get("inputs"))
        last: Msg | None = None
        async for event in next_handler(**input_kwargs):
            if isinstance(event, RequireExternalExecutionEvent):
                tool = event.tool_calls[0]
                self._emit(
                    agent,
                    Step.TOOL_START,
                    {
                        "tool_name": tool.name,
                        "tool_id": tool.id,
                        "input": tool.input,
                    },
                )
            elif isinstance(event, HintBlockEvent) and event.source == "steer":
                self._emit(agent, Step.STEER, {"text": _text(event.hint)})
            elif (
                isinstance(event, ReplyEndEvent)
                and event.finished_reason == ReplyFinishedReason.EXCEED_MAX_ITERS
            ):
                self._emit(agent, Step.EXCEED_MAX_ITERS, {})
            if isinstance(event, Msg) and event.role == "assistant":
                last = event
            yield event
        if last is not None:
            self._emit(agent, Step.ASSISTANT_REPLY, {"text": _text(last)})

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool = input_kwargs["tool_call"]
        info = {
            "tool_name": tool.name,
            "tool_id": tool.id,
            "input": tool.input,
        }
        started = time.monotonic()
        self._emit(agent, Step.TOOL_START, info)
        last = None
        async for item in next_handler(**input_kwargs):
            last = item
            yield item
        self._emit(
            agent,
            Step.TOOL_END,
            {
                **info,
                "state": last.state,
                "output": _text(last.content),
                "duration_ms": _ms(started),
            },
        )

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[
            ...,
            Awaitable[ChatResponse | AsyncGenerator[ChatResponse, None]],
        ],
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        model = input_kwargs["current_model"]
        started = time.monotonic()
        self._emit(agent, Step.MODEL_CALL_START, {"model_id": model.model})
        try:
            result = await next_handler(**input_kwargs)
        except Exception as exc:
            self._emit_model_failed(agent, started, exc)
            raise
        if hasattr(result, "__aiter__"):
            return self._wrap_model_stream(agent, result, started)
        self._log_model_end(agent, result)
        return result

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        context_size = agent.model.context_size
        cfg = input_kwargs.get("context_config") or agent.context_config
        tokens_before = await agent.model.count_tokens(
            **(await agent._prepare_model_input())
        )
        await next_handler(**input_kwargs)
        if tokens_before < cfg.trigger_ratio * context_size:
            return
        tokens_after = await agent.model.count_tokens(
            **(await agent._prepare_model_input())
        )
        self._emit(
            agent,
            Step.CONTEXT_COMPRESS,
            {
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "context_size": context_size,
            },
        )

    async def _wrap_model_stream(
        self,
        agent: Agent,
        result: AsyncGenerator[ChatResponse, None],
        started: float,
    ) -> AsyncGenerator[ChatResponse, None]:
        last: ChatResponse | None = None
        try:
            async for chunk in result:
                last = chunk
                yield chunk
        except Exception as exc:
            self._emit_model_failed(agent, started, exc)
            raise
        self._log_model_end(agent, last)

    def _log_model_end(self, agent: Agent, response: ChatResponse) -> None:
        usage = response.usage
        cancelled = response.finished_reason == FinishedReason.INTERRUPTED
        self._emit(
            agent,
            Step.MODEL_CALL_CANCEL if cancelled else Step.MODEL_CALL_END,
            {
                "model_request_id": response.id,
                "input_tokens": None if usage is None else usage.input_tokens,
                "context_size": agent.model.context_size,
                "duration_ms": None if usage is None else int(usage.time * 1000),
            },
        )
        if cancelled:
            raise asyncio.CancelledError

    def _emit_model_failed(self, agent: Agent, started: float, exc: Exception) -> None:
        self._emit(
            agent,
            Step.MODEL_CALL_FAILED,
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "duration_ms": _ms(started),
            },
            error=True,
        )

    def _log_entry(self, agent: Agent, inputs: Any) -> None:
        if isinstance(inputs, ExternalExecutionResultEvent):
            for result in inputs.execution_results:
                self._emit(
                    agent,
                    Step.TOOL_END,
                    {
                        "tool_name": result.name,
                        "tool_id": result.id,
                        "state": result.state,
                        "output": _text(result.output),
                    },
                )
            return
        for msg in inputs if isinstance(inputs, list) else [inputs]:
            if isinstance(msg, Msg) and msg.role == "user":
                self._emit(agent, Step.USER_INPUT, {"text": _text(msg)})

    def _emit(
        self,
        agent: Agent,
        step: Step,
        extra: dict[str, Any],
        *,
        error: bool = False,
    ) -> None:
        writer = log_error if error else log_info
        writer(
            request_id=self._ctx.chat_id,
            step=step,
            message=step.value,
            context={
                "session_id": self._ctx.session_id,
                "react_iter": agent.state.cur_iter,
                "react_max_iters": agent.react_config.max_iters,
                "reply_id": agent.state.reply_id,
                **extra,
            },
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "get_text_content"):
        return value.get_text_content() or ""
    if isinstance(value, list):
        return "\n".join(t for t in (getattr(b, "text", None) for b in value) if t)
    return str(value)
