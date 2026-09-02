# -*- coding: utf-8 -*-
"""Session / message content helpers used by settlement."""

from __future__ import annotations

from typing import Any


def extract_content_text(content: list[Any] | Any) -> str:
    """Join the text of content blocks; accepts a list or a single block."""
    if not content:
        return ""
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)
