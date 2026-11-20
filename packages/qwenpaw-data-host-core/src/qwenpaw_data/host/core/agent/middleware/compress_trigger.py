# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncGenerator, Awaitable, Callable

from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.settings import get_settings

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from agentscope.model import ChatResponse
    from qwenpaw_data.host.core.runtime.context import RunContext


class CompressTriggerMiddleware(MiddlewareBase):
    """Decide compression from the last model bill; lower trigger when over the line."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._ctx = run_context
        self._last_input_token: int | None = None
        self._last_output_token: int | None = None

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[
            ...,
            Awaitable[ChatResponse | AsyncGenerator[ChatResponse, None]],
        ],
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        _ = agent
        result = await next_handler(**input_kwargs)
        # not a stream
        if not hasattr(result, "__aiter__"):
            usage = result.usage
            if usage is None:
                raise ValueError("model usage is required")
            self._last_input_token = usage.input_tokens
            self._last_output_token = usage.output_tokens
            return result

        async def chunks() -> AsyncGenerator[ChatResponse, None]:
            last: ChatResponse | None = None
            async for chunk in result:
                last = chunk
                yield chunk
            if last is None:
                return
            usage = last.usage
            if usage is None:
                raise ValueError("model usage is required")
            self._last_input_token = usage.input_tokens
            self._last_output_token = usage.output_tokens

        return chunks()

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        context_size = agent.model.context_size
        if context_size is None or context_size <= 0:
            raise ValueError("context_size is required")
        runtime = get_settings().runtime
        user_trigger = self._ctx.user_runtime_config.context_trigger_ratio
        if (
            self._last_input_token is None
            or self._last_output_token is None
            or self._last_input_token + self._last_output_token
            < user_trigger * context_size
        ):
            await next_handler(**input_kwargs)
            return
        estimated = await agent.model.count_tokens(
            **(await agent._prepare_model_input())
        )
        cfg = agent.context_config
        temporary_trigger_ratio = min(
            runtime.context_max_trigger_ratio,
            max(
                cfg.reserve_ratio,
                runtime.context_occupancy_to_trigger_ratio * estimated / context_size,
            ),
        )
        await next_handler(
            **{
                **input_kwargs,
                "context_config": cfg.model_copy(
                    update={"trigger_ratio": temporary_trigger_ratio}
                ),
            }
        )
