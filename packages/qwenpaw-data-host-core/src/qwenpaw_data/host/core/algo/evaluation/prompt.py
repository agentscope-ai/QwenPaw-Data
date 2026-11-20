# -*- coding: utf-8 -*-
"""Load and assemble the grading prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_confidence_system_prompt() -> str:
    return (
        (Path(__file__).parent / "prompts" / "CONFIDENCE_SYSTEM_ZH.md")
        .read_text(encoding="utf-8")
        .strip()
    )


def build_confidence_user_prompt(record: str) -> str:
    return f"## 会话记录\n\n{record}\n"


__all__ = [
    "build_confidence_user_prompt",
    "get_confidence_system_prompt",
]
