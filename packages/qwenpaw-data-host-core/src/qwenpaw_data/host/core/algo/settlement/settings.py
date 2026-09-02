# -*- coding: utf-8 -*-
"""Settlement runtime configuration.

The model is not configured here: the manager builds it from the user's
preferences (light, then default) and falls back to the environment.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

_ENV_PREFIX = "QWENPAW_DATA_SETTLEMENT_"


def _env(name: str) -> str | None:
    raw = os.environ.get(f"{_ENV_PREFIX}{name.upper()}")
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class SettlementSettings(BaseModel):
    """Runtime configuration; every knob degrades to a safe default.

    Defaults come from ``QWENPAW_DATA_SETTLEMENT_*`` env vars; constructor
    kwargs win.
    """

    enabled: bool = Field(default_factory=lambda: _env_bool("enabled", True))
    window_size: int = Field(default_factory=lambda: _env_int("window_size", 3))
    pool_summary_limit: int = Field(
        default_factory=lambda: _env_int("pool_summary_limit", 20)
    )
    dismissed_summary_limit: int = Field(
        default_factory=lambda: _env_int("dismissed_summary_limit", 20)
    )
    confirmer_concurrency: int = Field(
        default_factory=lambda: _env_int("confirmer_concurrency", 4)
    )
    llm_timeout: float = Field(default_factory=lambda: _env_float("llm_timeout", 30.0))
    llm_attempts: int = Field(default_factory=lambda: _env_int("llm_attempts", 2))
    cm_timeout: float = Field(default_factory=lambda: _env_float("cm_timeout", 180.0))
