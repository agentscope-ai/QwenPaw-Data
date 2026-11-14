# -*- coding: utf-8 -*-
"""Runtime settings for evaluation."""

from __future__ import annotations

# Wait cap for one grading call.
CONFIDENCE_TIMEOUT_SECONDS = 30.0

# Max earlier segments the no-plan evidence looks back.
PRIOR_SEGMENTS = 4

# Recent typed inputs (current included) the no-plan evidence carries.
USER_INPUT_TURNS = 3

__all__ = [
    "CONFIDENCE_TIMEOUT_SECONDS",
    "PRIOR_SEGMENTS",
    "USER_INPUT_TURNS",
]
