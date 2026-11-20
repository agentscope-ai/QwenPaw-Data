# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from agentscope.agent import Agent
from agentscope.event import (
    ConfirmResult,
    EventType,
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import UserMsg
from qwenpaw_data.host.core.agent.middleware import (
    FollowUpMiddleware,
    TraceForwardMiddleware,
)
from qwenpaw_data.host.core.domain.clarification import ClarificationWithExecutor
from qwenpaw_data.host.core.domain.exception import IterationLimitExceeded
from qwenpaw_data.host.core.domain.tool_confirmation import ToolConfirmation
from qwenpaw_data.host.core.domain.ui import UiExecution
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input
from qwenpaw_data.host.core.tracing.adapters import tracer
from qwenpaw_data.host.core.utils.agent import close_interrupted_tool_calls
from qwenpaw_data.tracing.core.names import Tag

logger = logging.getLogger(__name__)


class AgentExecutor:
    """Drive one agent turn, including external clarification and steer."""

    def __init__(self, run_context: RunContext) -> None:
        self._run_context = run_context
        self._clarification = ClarificationWithExecutor(
            run_context.session_factory, run_context.chat_id
        )
        self._tool_confirmation = ToolConfirmation(
            run_context.session_factory,
            run_context.chat_id,
            level=run_context.user_runtime_config.tool_confirmation_level,
        )
        self._ui = UiExecution(run_context.session_factory, run_context.chat_id)
        self._task: asyncio.Task[None] | None = None
        self._agent: Agent | None = None
        self._after_event_callback: Callable[[Any], Awaitable[None]] | None = None

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

        # agentscope's own permission prompts (ask rules in a persisted
        # permission context) have no answering channel on a Host turn; tool
        # approval is ToolConfirmationMiddleware's job. Let the reply park,
        # then resume it with a denial instead of crashing the turn.
        confirmations: list[RequireUserConfirmEvent] = []
        while True:
            required: RequireExternalExecutionEvent | None = None
            # The chat trace id is bound here per iteration, not once in
            # ChatRuntime: this is the single place that needs it.
            async with tracer(self._run_context, tags=[Tag.CHAT]):
                events = agent.reply_stream(inputs)
                try:
                    async for event in events:
                        if isinstance(event, RequireExternalExecutionEvent):
                            required = event
                            break
                        if isinstance(event, RequireUserConfirmEvent):
                            confirmations.append(event)
                            continue

                        self._handle_tool_call_start(event)
                        await envelope.translate_event(event)
                        if self._after_event_callback is not None:
                            await self._after_event_callback(event)
                        if (
                            isinstance(event, ReplyEndEvent)
                            and event.finished_reason
                            == ReplyFinishedReason.EXCEED_MAX_ITERS
                        ):
                            raise IterationLimitExceeded(agent.name)
                finally:
                    await events.aclose()

            if required is not None:
                inputs = await self._handle_external_execution(required)
            elif confirmations:
                inputs = self._deny_confirmations(confirmations)
                confirmations = []
            else:
                return

    def _deny_confirmations(
        self, confirmations: list[RequireUserConfirmEvent]
    ) -> UserConfirmResultEvent:
        """Fail closed on every parked permission prompt of this reply."""
        tool_calls = [call for event in confirmations for call in event.tool_calls]
        logger.warning(
            "chat %s: denying %d unattended permission request(s): %s",
            self._run_context.chat_id,
            len(tool_calls),
            ", ".join(call.name for call in tool_calls),
        )
        return UserConfirmResultEvent(
            reply_id=confirmations[-1].reply_id,
            confirm_results=[
                ConfirmResult(confirmed=False, tool_call=call) for call in tool_calls
            ],
        )

    async def _handle_external_execution(
        self,
        required: RequireExternalExecutionEvent,
    ) -> ExternalExecutionResultEvent:
        tool_call = required.tool_calls[0]
        result = await self._clarification.wait_for_answer(
            tool_call.id, tool_input=tool_call.input
        )
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
        self._tool_confirmation.add_metadata(
            tool_name=event.tool_call_name,
            metadata=event.metadata,
        )
        self._ui.add_metadata(
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
        self._after_event_callback = after_event_callback
        self._task = asyncio.create_task(self.run(agent, turn, envelope))
        return self._task

    async def stop(self) -> None:
        """Cancel the current agent execution, close open tool calls, end the trace."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._agent is not None:
            close_interrupted_tool_calls(self._agent)
            forward = TraceForwardMiddleware.find(self._agent)
            if forward is not None:
                await forward.end_stream()
            followup = FollowUpMiddleware.find(self._agent)
            if followup is not None:
                await followup.end_stream()
