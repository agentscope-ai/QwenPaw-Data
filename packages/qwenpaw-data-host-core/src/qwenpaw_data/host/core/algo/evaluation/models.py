# -*- coding: utf-8 -*-
"""Verdict models for confidence grading."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceLevel = Literal["high", "medium", "low"]


class ConfidenceResult(BaseModel):
    """The grading verdict: one overall level for the segment, and why."""

    model_config = ConfigDict(extra="forbid")

    level: ConfidenceLevel
    summary: str = Field(min_length=1)


__all__ = [
    "ConfidenceLevel",
    "ConfidenceResult",
]
