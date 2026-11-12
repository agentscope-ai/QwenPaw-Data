# -*- coding: utf-8 -*-
"""Algo-side defaults (Host injects operator knobs as arguments)."""

from __future__ import annotations

USER_FEEDBACK_THRESHOLD = 1
TIMEOUT_SEC = 120.0
MAX_PATCHES = 8
MAX_FILES = 12
MAX_SKILL_CHARS = 24_000
MAX_TRACE_CHARS = 20_000

__all__ = [
    "MAX_FILES",
    "MAX_PATCHES",
    "MAX_SKILL_CHARS",
    "MAX_TRACE_CHARS",
    "USER_FEEDBACK_THRESHOLD",
    "TIMEOUT_SEC",
]
