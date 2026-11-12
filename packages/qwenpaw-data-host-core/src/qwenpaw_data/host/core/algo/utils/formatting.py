# -*- coding: utf-8 -*-
"""Shared text helpers for card bodies, window rendering and evidence."""

from __future__ import annotations

import json
import re
from typing import Any

_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/(?:.*/)?([^/]+)/SKILL\.md$", re.IGNORECASE)
_HASH_TEMP_RE = re.compile(r"^[0-9a-f]{8,}\.[A-Za-z0-9]+$", re.IGNORECASE)
_NUMBER_COLOR_RE = re.compile(r"\{(blue|red|green):([^{}]+)\}")
_NUMBER_COLOR_CLASS = {
    "blue": "text-blue-600 font-bold",
    "red": "text-red-500 font-bold",
    "green": "text-green-600 font-bold",
}
FILE_PATH_KEYS = ("file_path", "path", "filename", "target_file")


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


def cap_middle(text: str, cap: int, mark: str) -> str:
    """Keep the head and tail of an over-budget string, inserting ``mark``."""
    if len(text) <= cap:
        return text
    keep = max(cap // 2 - 40, 200)
    return text[:keep] + mark + text[-keep:]


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


def file_basename(path: str | None) -> str:
    """Return the last path segment, or an empty string."""
    if not path:
        return ""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def skill_name(path: str | None) -> str | None:
    """Return the skill name when ``path`` targets ``skills/.../<name>/SKILL.md``."""
    if not path:
        return None
    match = _SKILL_PATH_RE.search(path.replace("\\", "/"))
    return match.group(1) if match else None


def is_hash_temp_name(name: str) -> bool:
    """Whether ``name`` looks like a temporary download hash (e.g. ``95f204a5.csv``)."""
    return bool(name) and _HASH_TEMP_RE.fullmatch(name) is not None


def expand_number_colors(text: str) -> str:
    """Rewrite ``{blue:28.71}`` markers into allowlisted number-color spans."""

    def replace(match: re.Match[str]) -> str:
        cls = _NUMBER_COLOR_CLASS[match.group(1)]
        return f'<span class="{cls}">{match.group(2)}</span>'

    return _NUMBER_COLOR_RE.sub(replace, text)


def is_business_file(path: str | None) -> bool:
    """Whether ``path`` is a named business file, not a skill doc or hash temp."""
    name = file_basename(path)
    if not name:
        return False
    if skill_name(path) or name.lower() == "skill.md":
        return False
    return not is_hash_temp_name(name)


__all__ = [
    "FILE_PATH_KEYS",
    "cap_middle",
    "compact",
    "expand_number_colors",
    "file_basename",
    "first_str",
    "is_business_file",
    "is_hash_temp_name",
    "join_sections",
    "output_text",
    "skill_name",
    "truncate",
]
