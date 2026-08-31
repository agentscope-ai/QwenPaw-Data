# -*- coding: utf-8 -*-
"""Inject pending Steer hints at the on_reasoning safe boundary."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from agentscope.agent import Agent
from agentscope.event import HintBlockEvent
from agentscope.message import AssistantMsg, HintBlock
from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.domain.steer import SteerQueue


class SteerMiddleware(MiddlewareBase):
    """Own the SteerQueue; drain it before each reasoning step."""

    def __init__(self) -> None:
        self.queue = SteerQueue()

    @classmethod
    def find(cls, agent: Agent) -> SteerMiddleware | None:
        for middleware in agent._reasoning_middlewares:
            if isinstance(middleware, cls):
                return middleware
        return None

    @classmethod
    def require(cls, agent: Agent) -> SteerMiddleware:
        middleware = cls.find(agent)
        if middleware is None:
            raise RuntimeError("CONFLICT: SteerMiddleware is not configured")
        return middleware

    def reset(self) -> None:
        """Start a fresh queue; cancel_all() closes a queue terminally."""
        self.queue = SteerQueue()

    async def steer(self, text: str) -> None:
        """Enqueue steer text and wait until it is injected."""
        await self.queue.wait_until_injected(text)

    async def cancel(self) -> None:
        """Release every pending steer waiter."""
        await self.queue.cancel_all()

    async def on_reasoning(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        pending = await self.queue.take_pending()
        if pending:
            hint_blocks = [
                HintBlock(hint=item.text, source="steer") for item in pending
            ]
            self._append_hints(agent, hint_blocks)
            for item in pending:
                await self.queue.mark_injected(item)
            for hint in hint_blocks:
                yield HintBlockEvent(
                    reply_id=agent.state.reply_id,
                    block_id=hint.id,
                    source="steer",
                    hint=hint.hint,
                )

        async for event in next_handler(**input_kwargs):
            yield event

    @staticmethod
    def _append_hints(agent: Agent, hint_blocks: list[HintBlock]) -> None:
        if agent.state.context:
            last_msg = agent.state.context[-1]
            if last_msg.role == "assistant" and last_msg.name == agent.name:
                last_msg.content.extend(hint_blocks)
                return
        agent.state.context.append(
            AssistantMsg(
                id=agent.state.reply_id,
                name=agent.name,
                content=list(hint_blocks),
            ),
        )
