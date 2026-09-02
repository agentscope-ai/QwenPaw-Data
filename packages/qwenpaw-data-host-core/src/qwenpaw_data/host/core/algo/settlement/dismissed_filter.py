# -*- coding: utf-8 -*-
"""DismissedFilter: drop items semantically equal to cards the user dismissed."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM

from .calls import StructuredCallError, structured_call
from .models import DetectedItem, DismissedFilterResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "DISMISSED_FILTER.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class DismissedFilter:
    """LLM-judged dedupe of candidate items against dismissed cards."""

    def __init__(
        self, llm: StructuredLLM, *, system_prompt: str | None = None
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt or _load_prompt()

    async def filter(
        self,
        items: list[DetectedItem],
        dismissed_cards: list[dict[str, Any]],
    ) -> list[DetectedItem]:
        """Return the subset not equivalent to a dismissed card.

        When the LLM fails, the whole batch is dropped: uncertain → skip.
        """
        if not dismissed_cards:
            return items

        user_content = self._build_user_message(items, dismissed_cards)
        try:
            result = await structured_call(
                self._llm,
                system=self._system_prompt,
                user=user_content,
                schema=DismissedFilterResult,
            )
            removed = set(result.dismissed_indices)
            kept = [item for i, item in enumerate(items) if i not in removed]
            if removed:
                logger.info("Dismissed filter: removed indices %s", sorted(removed))
            return kept
        except StructuredCallError:
            logger.warning(
                "Dismissed filter LLM failed, dropping all items (uncertain → skip)",
                exc_info=True,
            )
            return []

    @staticmethod
    def _build_user_message(
        items: list[DetectedItem],
        dismissed_cards: list[dict[str, Any]],
    ) -> str:
        parts = ["## 待推荐项"]
        for i, item in enumerate(items):
            parts.append(f"[{i}] type={item.type.value} fields={item.fields}")
        parts.append("")
        parts.append("## 已拒绝卡片")
        for card in dismissed_cards:
            parts.append(f"- type={card['type']} fields={card['fields']}")
        return "\n".join(parts)
