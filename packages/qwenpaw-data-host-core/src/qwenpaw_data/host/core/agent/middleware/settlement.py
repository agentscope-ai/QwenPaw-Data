# -*- coding: utf-8 -*-
"""Confirmed settlement cards appended to the agent's system prompt."""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from agentscope.middleware import MiddlewareBase

logger = logging.getLogger(__name__)

CardLoader = Callable[[str], Awaitable[list[dict[str, Any]]]]


class ConfirmedSettlementPromptMiddleware(MiddlewareBase):
    """Append the session's confirmed settlement cards to the system prompt.

    ``loader`` maps a session id to its confirmed cards; failures degrade to
    an unmodified prompt so settlement can never break a turn.
    """

    def __init__(self, *, loader: CardLoader) -> None:
        self._loader = loader

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        session_id = getattr(agent, "session_id", None)
        if not session_id:
            return current_prompt
        try:
            cards = await self._loader(session_id)
        except Exception:
            logger.warning(
                "settlement: failed to load confirmed cards for session %s",
                session_id,
                exc_info=True,
            )
            return current_prompt
        if not cards:
            return current_prompt
        return f"{current_prompt}\n\n{self.format_cards(cards)}"

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
