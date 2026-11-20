# -*- coding: utf-8 -*-
"""Inject pending Steer hints at the on_reasoning safe boundary."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from agentscope.agent import Agent
from agentscope.event import HintBlockEvent
from agentscope.message import AssistantMsg, HintBlock
from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.tracing.adapters import tracer_span
from qwenpaw_data.host.core.repositories import ChatCommandRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.turn import compose_agent_input
from qwenpaw_data.tracing.core.names import Step


class SteerMiddleware(MiddlewareBase):
    def __init__(self, run_context: RunContext) -> None:
        self._session_factory = run_context.session_factory
        self._chat_id = run_context.chat_id

    def _steer_span(
        self, state: str, steers: list[dict] | None = None, error: str = ""
    ) -> None:
        with tracer_span(
            Step.STEER,
            attributes={"steer.count": len(steers)} if steers else None,
            metadata={"steer_state": state},
            input=[
                {
                    "text": str(s["text"]),
                    "artifact_comments": list(s["artifact_comments"]),
                }
                for s in steers
            ]
            if steers
            else None,
            error=error or None,
        ):
            pass

    async def on_reasoning(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        try:
            async with self._session_factory() as db:
                pending = await ChatCommandRepository(db).take_steers(self._chat_id)
                await db.commit()
        except Exception as exc:
            self._steer_span("error", error=type(exc).__name__)
            raise
        if pending:
            hint_blocks = [
                HintBlock(
                    hint=compose_agent_input(
                        str(steer["text"]),
                        list(steer["artifact_comments"]),
                    ),
                    source="steer",
                )
                for steer in pending
            ]
            self._append_hints(agent, hint_blocks)
            self._steer_span("success", steers=list(pending))
            for hint, steer in zip(hint_blocks, pending):
                yield HintBlockEvent(
                    reply_id=agent.state.reply_id,
                    block_id=hint.id,
                    source="steer",
                    hint=str(steer["text"]),
                    metadata={"artifact_comments": list(steer["artifact_comments"])},
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
