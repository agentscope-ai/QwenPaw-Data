# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.envelope import Envelope


class ContextUsageMiddleware(MiddlewareBase):
    """Each reasoning: count tokens the same way compression does, then emit SSE."""

    def __init__(self, *, envelope: Envelope) -> None:
        self._envelope = envelope

    async def on_reasoning(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        context_size = agent.model.context_size
        if context_size is None or context_size <= 0:
            raise ValueError("context_size is required")
        cfg = agent.context_config
        kwargs = await agent._prepare_model_input()
        used_tokens = await agent.model.count_tokens(**kwargs)
        await self._envelope.stream.context_usage(
            context_size=int(context_size),
            used_tokens=int(used_tokens),
            trigger_ratio=float(cfg.trigger_ratio),
            reserve_ratio=float(cfg.reserve_ratio),
            tool_result_limit=int(cfg.tool_result_limit),
        )
        async for event in next_handler(**input_kwargs):
            yield event
