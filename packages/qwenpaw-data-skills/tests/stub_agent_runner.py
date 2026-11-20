# -*- coding: utf-8 -*-
"""测试替身：返回预设文本并记录调用参数的 Agent 运行器。

``GoldImageReader`` 只要求 runner 提供 ``ask(prompt, *, images=...)``。这里不触达
真实模型，做两件对断言有用的事：按技能名或提示词子串回 canned 文本，以及把每次
调用的入参记进 ``_calls``，让测试能确认图片确实被透传、提示词带了章节上下文。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class StubAgentRunner:
    """按提示词关键字回预设文本，未命中时回声提示词。"""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}
        self._calls: list[dict[str, Any]] = []

    def ask(
        self,
        prompt: str,
        *,
        skill: str | None = None,
        session_id: str | None = None,
        request_context: dict | None = None,
        images: list[Path] | None = None,
        internal: bool = False,
    ) -> str:
        self._calls.append(
            {
                "prompt": prompt,
                "skill": skill,
                "session_id": session_id,
                "request_context": request_context,
                "images": list(images) if images else [],
                "internal": internal,
            }
        )
        # 技能名命中优先，其次按提示词子串匹配。
        if skill and skill in self._responses:
            return self._responses[skill]
        for key, resp in self._responses.items():
            if key in prompt:
                return resp
        return f"[stub] echo: {prompt[:200]}"
