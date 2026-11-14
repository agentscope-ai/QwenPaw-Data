# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClarificationSettings(BaseSettings):
    """Clarification Host settings."""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_CLARIFICATION_", extra="ignore"
    )

    hint_ttl_seconds: int = 1800
    questions_min_items: int = 1
    questions_max_items: int = 4
    options_min_items: int = 2
    options_max_items: int = 4

    @field_validator(
        "hint_ttl_seconds",
        "questions_min_items",
        "questions_max_items",
        "options_min_items",
        "options_max_items",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("questions_max_items")
    @classmethod
    def _questions_max_ge_min(cls, value: int, info) -> int:  # noqa: ANN001
        minimum = info.data.get("questions_min_items")
        if minimum is not None and value < minimum:
            raise ValueError("questions_max_items must be >= questions_min_items")
        return value

    @field_validator("options_max_items")
    @classmethod
    def _options_max_ge_min(cls, value: int, info) -> int:  # noqa: ANN001
        minimum = info.data.get("options_min_items")
        if minimum is not None and value < minimum:
            raise ValueError("options_max_items must be >= options_min_items")
        return value
