# -*- coding: utf-8 -*-
"""Cap one toolkit.call_tool; timeout returns ERROR and drops partial chunks."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext


def timeout_error(name: str, seconds: float) -> str:
    """Structured TOOL_TIMEOUT tool error, same JSON shape as 0.3."""
    return json.dumps(
        {
            "error": {
                "code": "TOOL_TIMEOUT",
                "tool": name,
                "timeout_seconds": seconds,
                "message": (
                    "Tool execution timed out. No complete result was accepted; "
                    "do not claim success."
                ),
            },
        },
        ensure_ascii=False,
    )


class ToolTimeoutMiddleware(MiddlewareBase):
    """Bound one acting call; skip when tool_timeout_enabled is false."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._ctx = run_context

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        _ = agent
        cfg = self._ctx.user_runtime_config
        seconds = cfg.tool_timeout_seconds
        name = str(getattr(input_kwargs["tool_call"], "name", "") or "")
        if not cfg.tool_timeout_enabled:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        async def _collect() -> list[Any]:
            return [item async for item in next_handler(**input_kwargs)]

        task = asyncio.create_task(_collect())
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                yield ToolResponse(
                    content=[TextBlock(text=timeout_error(name, seconds))],
                    state=ToolResultState.ERROR,
                )
                return
            items = task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        for item in items:
            yield item
