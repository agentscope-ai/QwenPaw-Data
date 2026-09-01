# -*- coding: utf-8 -*-
"""Shared text helpers for card bodies and window rendering."""

from __future__ import annotations

import json
from typing import Any


def compact(value: Any) -> str:
    """Render any tool payload as one compact line of text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def output_text(value: Any) -> str:
    """Flatten a tool output into text, keeping only human-readable parts."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                parts.append(text if isinstance(text, str) else compact(block))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return compact(value)


def truncate(text: str, cap: int) -> str:
    """Trim to ``cap`` characters, marking the cut with an ellipsis."""
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[: cap - 3] + "..."


def join_sections(*parts: str) -> str:
    """Join non-empty markdown sections with a blank line between them."""
    return "\n\n".join(part for part in parts if part)


def first_str(payload: Any, keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value among ``keys``."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "compact",
    "first_str",
    "join_sections",
    "output_text",
    "truncate",
]
