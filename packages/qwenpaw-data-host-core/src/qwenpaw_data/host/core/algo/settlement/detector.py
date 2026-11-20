# -*- coding: utf-8 -*-
"""SettlementDetector：识别对话中可沉淀的语义层知识。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentscope.message import SystemMsg, UserMsg
from agentscope.model import ChatModelBase
from qwenpaw_data.tracing.core.names import Step

from .llm import DEFAULT_ATTEMPTS, DEFAULT_TIMEOUT, StructuredCallError, structured_call
from .models import DetectionResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "DETECTOR.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class SettlementDetector:
    """从对话中识别可沉淀的语义层知识。"""

    def __init__(
        self, model: ChatModelBase, *, system_prompt: str | None = None
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt or _load_prompt()

    async def detect(
        self,
        recent_turns: list[dict[str, Any]],
        pool_summary: list[dict[str, Any]],
        *,
        domain_names: list[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        attempts: int = DEFAULT_ATTEMPTS,
    ) -> DetectionResult | None:
        """执行检测。返回 None 表示 LLM 调用失败。"""
        user_content = self._build_user_message(
            recent_turns,
            pool_summary,
            domain_names=domain_names,
        )
        messages = [
            SystemMsg(name="system", content=self._system_prompt),
            UserMsg(name="user", content=user_content),
        ]
        try:
            return await structured_call(
                self._model,
                messages,
                DetectionResult,
                timeout=timeout,
                attempts=attempts,
                source_step=Step.SETTLEMENT_DETECT,
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
                parts.append(
                    f"- id={item['id']} type={item['type']} fields={item['fields']}"
                )
            parts.append("")

        return "\n".join(parts)
