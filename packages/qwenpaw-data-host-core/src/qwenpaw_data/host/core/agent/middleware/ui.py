# -*- coding: utf-8 -*-
"""Pause a UI tool call until the frontend returns the page result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.domain.ui import UI_TOOL_NAMES, UiExecution

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext


class UiMiddleware(MiddlewareBase):
    def __init__(self, *, run_context: RunContext) -> None:
        self._ui = UiExecution(run_context.session_factory, run_context.chat_id)

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        _ = agent
        tool = input_kwargs["tool_call"]
        if tool.name not in UI_TOOL_NAMES:
            async for item in next_handler(**input_kwargs):
                yield item
            return
        yield UiExecution.response(await self._ui.wait(tool.id))
