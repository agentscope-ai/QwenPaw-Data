# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from agentscope.agent import Agent
from agentscope.event import (
    EventType,
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
)
from agentscope.message import UserMsg

from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.domain.clarification import ClarificationWithExecutor
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input
from qwenpaw_data.host.core.utils.agent import close_interrupted_tool_calls


class AgentExecutor:
    """Drive one agent turn, including external clarification and steer."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._agent: Agent | None = None
        self._clarification = ClarificationWithExecutor()
        self._after_event_callback: Callable[[Any], Awaitable[None]] | None = None

    def answer_clarification(
        self,
        *,
        clarification_id: str,
        result: dict[str, Any],
    ) -> None:
        self._clarification.answer(
            clarification_id=clarification_id,
            result=result,
        )

    async def steer(
        self,
        text: str,
        artifact_comments: list[dict] | None = None,
    ) -> None:
        """Enqueue steer text and wait until it is injected into the agent."""
        if self._agent is None:
            raise RuntimeError("CONFLICT: chat runtime agent is not ready")
        await SteerMiddleware.require(self._agent).steer(text, artifact_comments)

    async def cancel_steer(self) -> None:
        """Release pending steer waiters when SteerMiddleware is present."""
        if self._agent is None:
            return
        middleware = SteerMiddleware.find(self._agent)
        if middleware is not None:
            await middleware.cancel()

    async def run(
        self,
        agent: Agent,
        turn: TurnInput,
        envelope: Envelope,
    ) -> None:
        inputs: Any = UserMsg(
            name="user",
            content=compose_agent_input(
                turn.user_input,
                turn.artifact_comments,
                turn.attachments,
                session_id=turn.session_id,
            ),
        )

        while True:
            required: RequireExternalExecutionEvent | None = None
            events = agent.reply_stream(inputs)
            try:
                async for event in events:
                    if isinstance(event, RequireExternalExecutionEvent):
                        required = event
                        break

                    self._handle_tool_call_start(event)
                    await envelope.translate_event(event)
                    if self._after_event_callback is not None:
                        await self._after_event_callback(event)
            finally:
                await events.aclose()

            if required is None:
                return
            inputs = await self._handle_external_execution(required)

    async def _handle_external_execution(
        self,
        required: RequireExternalExecutionEvent,
    ) -> ExternalExecutionResultEvent:
        tool_call = required.tool_calls[0]
        result = await self._clarification.wait_for_answer(tool_call.id)
        if ClarificationWithExecutor.is_timeout(result):
            raise asyncio.CancelledError
        return ExternalExecutionResultEvent(
            reply_id=required.reply_id,
            execution_results=[result],
        )

    def _handle_tool_call_start(self, event: Any) -> None:
        if event.type != EventType.TOOL_CALL_START:
            return
        self._clarification.add_metadata(
            tool_name=event.tool_call_name,
            metadata=event.metadata,
        )

    def start(
        self,
        agent: Agent,
        turn: TurnInput,
        envelope: Envelope,
        *,
        after_event_callback: Callable[[Any], Awaitable[None]] | None = None,
    ) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("executor already running")
        self._agent = agent
        # The host caches its agent across turns; a fresh queue per turn
        # undoes the terminal close performed by the previous stop().
        middleware = SteerMiddleware.find(agent)
        if middleware is not None:
            middleware.reset()
        self._after_event_callback = after_event_callback
        self._task = asyncio.create_task(self.run(agent, turn, envelope))
        return self._task

    async def stop(self) -> None:
        """Cancel the current agent execution and close open tool calls."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._agent is not None:
            close_interrupted_tool_calls(self._agent)
            await self.cancel_steer()
