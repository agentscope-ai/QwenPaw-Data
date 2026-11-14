# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_LOG_", extra="ignore")

    path: Path = Path("logs/query.log")
    context_max_chars: int = 8000
    queue_maxsize: int = 8192
    name: str = "qwenpaw_data.query"
    level: str = "INFO"
    # Local file is a buffer for Logtail; SLS is the long-term store.
    max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    backup_count: int = Field(default=2, ge=1)
