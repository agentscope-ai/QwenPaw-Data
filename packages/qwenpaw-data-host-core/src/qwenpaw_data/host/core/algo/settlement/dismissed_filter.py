# -*- coding: utf-8 -*-
"""DismissedFilter：LLM 判断待推荐项是否与已拒绝卡片语义等价。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentscope.message import SystemMsg, UserMsg
from agentscope.model import ChatModelBase
from qwenpaw_data.tracing.core.names import Step

from .llm import DEFAULT_ATTEMPTS, DEFAULT_TIMEOUT, StructuredCallError, structured_call
from .models import DetectedItem, DismissedFilterResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "DISMISSED_FILTER.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class DismissedFilter:
    """判断待推荐项是否与已拒绝卡片语义等价，过滤掉等价项。"""

    def __init__(
        self, model: ChatModelBase, *, system_prompt: str | None = None
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt or _load_prompt()

    async def filter(
        self,
        items: list[DetectedItem],
        dismissed_cards: list[dict[str, Any]],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        attempts: int = DEFAULT_ATTEMPTS,
    ) -> list[DetectedItem]:
        """返回去除已拒绝等价项后的子集。LLM 失败时整批不推（不确定不推）。"""
        if not dismissed_cards:
            return items

        user_content = self._build_user_message(items, dismissed_cards)
        messages = [
            SystemMsg(name="system", content=self._system_prompt),
            UserMsg(name="user", content=user_content),
        ]
        try:
            result = await structured_call(
                self._model,
                messages,
                DismissedFilterResult,
                timeout=timeout,
                attempts=attempts,
                source_step=Step.SETTLEMENT_DISMISSED_FILTER,
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
