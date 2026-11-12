# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TracingSettings(BaseSettings):
    """Host-only knobs. Shared TRACING_* fields live on Settings.from_env()."""

    model_config = SettingsConfigDict(env_prefix="TRACING_", extra="ignore")

    flush_interval_seconds: float = Field(default=5.0, gt=0)
