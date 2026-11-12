# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class FollowUpSettings(BaseSettings):
    """Follow-up recommendation Host settings.

    The defaults are restated here rather than imported from the algorithm:
    reading them from ``algo.followup`` would make ``get_settings()`` import the
    whole recommender, whose skill index scans the workspace at import time.
    They must stay in step with that package's own constants.
    """

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_FOLLOWUP_", extra="ignore")

    enabled: bool = True
    # What the Chat's closing moment is willing to wait for; on overrun the
    # reply is released and the questions arrive on the next snapshot instead.
    timeout_sec: float = 2.0
    max_questions: int = 3
    # Entity pruning: how much material may enter the prompt, and how close to
    # the turn's anchor metric an entity has to be to earn a place in it.
    max_metrics: int = 6
    max_dimensions: int = 8
    min_relevance: float = 0.3


__all__ = ["FollowUpSettings"]
