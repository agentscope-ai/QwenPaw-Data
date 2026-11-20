# -*- coding: utf-8 -*-
"""Session / message content helpers used by settlement."""

from __future__ import annotations

from typing import Any


def extract_content_text(content: list[Any] | Any) -> str:
    """从 content blocks 中拼接文本。兼容 list[Block] 或单个对象。"""
    if not content:
        return ""
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)
