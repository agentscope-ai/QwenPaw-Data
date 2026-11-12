# -*- coding: utf-8 -*-
"""Settlement detection after reply; confirmed cards in system prompt."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.event import ReplyEndEvent
from agentscope.middleware import MiddlewareBase
from qwenpaw_data.host.core.repositories import SettlementRepository
from qwenpaw_data.host.core.tracing.adapters import tracer
from qwenpaw_data.tracing.core.names import Step, Tag

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext
    from qwenpaw_data.host.core.runtime.envelope import Envelope

logger = logging.getLogger(__name__)


class SettlementDetectionMiddleware(MiddlewareBase):
    """Schedule settlement detection when a reply ends."""

    def __init__(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
    ) -> None:
        self._run_context = run_context
        self._envelope = envelope

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        ended = False
        try:
            async for event in next_handler():
                if isinstance(event, ReplyEndEvent):
                    ended = True
                yield event
        finally:
            if ended:
                asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            from qwenpaw_data.host.core.algo.settlement import SettlementManager

            async with tracer(
                self._run_context, Step.SETTLEMENT, tags=[Tag.SETTLEMENT]
            ):
                async with self._envelope._session_factory() as db:
                    mgr = SettlementManager(db, identity=self._envelope.stream.identity)
                    await mgr.on_chat_finish(
                        chat_id=self._run_context.chat_id,
                        session_id=self._run_context.session_id,
                    )
                    await db.commit()
        except Exception:
            logger.warning(
                "Settlement detection failed for chat %s",
                self._run_context.chat_id,
                exc_info=True,
            )


class SkillEvolutionDetectionMiddleware(SettlementDetectionMiddleware):
    """Schedule skill evolution when a reply ends."""

    async def _run(self) -> None:
        try:
            from qwenpaw_data.host.core.algo.skill_evolution import SkillEvolutionManager

            async with tracer(
                self._run_context, Step.SKILL_EVOLUTION, tags=[Tag.SKILL_EVOLUTION]
            ):
                await SkillEvolutionManager.after_reply(
                    self._envelope._session_factory,
                    identity=self._envelope.stream.identity,
                    paths=self._run_context.paths,
                    chat_id=self._run_context.chat_id,
                    session_id=self._run_context.session_id,
                )
        except Exception:
            logger.warning(
                "Skill evolution failed for chat %s",
                self._run_context.chat_id,
                exc_info=True,
            )


class ConfirmedSettlementPromptMiddleware(MiddlewareBase):
    """Append session confirmed settlement cards to the system prompt.

    Loading or formatting failures degrade to the unmodified prompt (with a
    warning) so settlement can never break a turn or leave a partial block.
    """

    def __init__(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
    ) -> None:
        self._run_context = run_context
        self._envelope = envelope

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        _ = agent
        try:
            async with self._envelope._session_factory() as db:
                cards = await SettlementRepository(
                    db, self._envelope.stream.identity
                ).list_by_session(
                    self._run_context.session_id,
                    status="confirmed",
                )
            if not cards:
                return current_prompt
            block = self.format_cards(cards)
        except Exception:
            logger.warning(
                "settlement: failed to load confirmed cards for session %s",
                self._run_context.session_id,
                exc_info=True,
            )
            return current_prompt
        return f"{current_prompt}\n\n{block}"

    @staticmethod
    def format_cards(cards: list[dict[str, Any]]) -> str:
        lines = [
            "- type={type} fields={fields}".format(
                type=card["type"],
                fields=json.dumps(
                    card["fields"],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            for card in cards
        ]
        return "\n".join(
            [
                "<confirmed_settlement_cards>",
                *lines,
                "</confirmed_settlement_cards>",
            ]
        )
