"""Bound complete async tool execution, including MCP and artifact handling."""

from __future__ import annotations

import asyncio
import json
import math
import os

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse


class ToolTimeoutMiddleware(MiddlewareBase):
    """Cooperative cancellation; remote work may require connector-side limits."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("QWENPAW_DATA_TOOL_TIMEOUT_SECONDS", "1800")
        )
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive and finite")

    async def on_acting(self, agent, input_kwargs, next_handler):
        seconds = self.timeout_seconds
        name = getattr(input_kwargs["tool_call"], "name", "")

        async def collect():
            return [item async for item in next_handler(**input_kwargs)]

        task = asyncio.create_task(collect())
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                yield ToolResponse(
                    state=ToolResultState.ERROR,
                    content=[
                        TextBlock(
                            text=json.dumps(
                                {
                                    "error": {
                                        "code": "TOOL_TIMEOUT",
                                        "tool": name,
                                        "timeout_seconds": seconds,
                                        "message": "Tool execution timed out. No complete result was accepted; do not claim success.",
                                    },
                                }
                            )
                        )
                    ],
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
