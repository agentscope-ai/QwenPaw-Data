# -*- coding: utf-8 -*-
"""Pause a tool call until the user approves, or fail it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.domain.tool_confirmation import ToolConfirmation

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext


class ToolConfirmationMiddleware(MiddlewareBase):
    def __init__(self, *, run_context: RunContext) -> None:
        self._gate = ToolConfirmation(
            run_context.session_factory,
            run_context.chat_id,
            level=run_context.user_runtime_config.tool_confirmation_level,
        )

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        _ = agent
        tool = input_kwargs["tool_call"]
        name = tool.name
        if self._gate.decide(name) == "allow":
            async for item in next_handler(**input_kwargs):
                yield item
            return
        answer = await self._gate.wait(tool.id)
        if answer == "approved":
            async for item in next_handler(**input_kwargs):
                yield item
            return
        if answer == "denied":
            reason = "user denied"
        elif answer == "timeout":
            reason = "user confirmation timed out"
        else:
            raise ValueError(f"invalid tool confirmation answer: {answer!r}")
        yield ToolConfirmation.denied_response(name, reason=reason)
