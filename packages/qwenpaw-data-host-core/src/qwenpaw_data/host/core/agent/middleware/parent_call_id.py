# -*- coding: utf-8 -*-
from __future__ import annotations

from contextvars import ContextVar
from contextlib import aclosing
from typing import Any, AsyncGenerator, Callable

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase

_PARENT_CALL_ID: ContextVar[str] = ContextVar("spawn_subagent_parent_call_id")
_SPAWN_TOOL_NAME = "spawn_subagent"


def parent_call_id() -> str:
    return _PARENT_CALL_ID.get()


class BindParentCallIdMiddleware(MiddlewareBase):
    """Expose the current tool_call.id to spawn_subagent."""

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        _ = agent
        tool_call = input_kwargs["tool_call"]
        if tool_call.name != _SPAWN_TOOL_NAME:
            async for item in next_handler(**input_kwargs):
                yield item
            return
        # Never hold a token across a yield: the SDK may resume or close the
        # iterator from another task. Each advance binds just this tool call.
        async with aclosing(next_handler(**input_kwargs)) as events:
            while True:
                token = _PARENT_CALL_ID.set(str(tool_call.id))
                try:
                    item = await anext(events)
                except StopAsyncIteration:
                    return
                finally:
                    _PARENT_CALL_ID.reset(token)
                yield item
