# -*- coding: utf-8 -*-
"""SettlementDetector: find semantic-layer knowledge worth settling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM

from .calls import StructuredCallError, structured_call
from .models import DetectionResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "DETECTOR.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class SettlementDetector:
    """Extract candidate settlement items from recent conversation turns."""

    def __init__(
        self, llm: StructuredLLM, *, system_prompt: str | None = None
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt or _load_prompt()

    async def detect(
        self,
        recent_turns: list[dict[str, Any]],
        pool_summary: list[dict[str, Any]],
        *,
        domain_names: list[str] | None = None,
    ) -> DetectionResult | None:
        """Run detection; None means the LLM call failed."""
        user_content = self._build_user_message(
            recent_turns, pool_summary,
            domain_names=domain_names,
        )
        try:
            return await structured_call(
                self._llm,
                system=self._system_prompt,
                user=user_content,
                schema=DetectionResult,
            )
        except StructuredCallError:
            logger.warning("detection failed, returning None", exc_info=True)
            return None

    def _build_user_message(
        self,
        recent_turns: list[dict[str, Any]],
        pool_summary: list[dict[str, Any]],
        *,
        domain_names: list[str] | None = None,
    ) -> str:
        parts: list[str] = []

        if domain_names:
            parts.append("## 可用业务域")
            parts.append("每条 item 的 domain 必须从下列选一个，原文一致：")
            for name in domain_names:
                parts.append(f"- {name}")
            parts.append("")

        if recent_turns:
            parts.append("## 对话上下文")
            for turn in recent_turns:
                role = turn.get("role", "?")
                content = turn.get("content", "")
                parts.append(f"[{role}] {content}")
            parts.append("")

        if pool_summary:
            parts.append("## 本会话已提取过的项（语义相同则勿再提取）")
            for item in pool_summary:
                parts.append(f"- id={item['id']} type={item['type']} fields={item['fields']}")
            parts.append("")

        return "\n".join(parts)
