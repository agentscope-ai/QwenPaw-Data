# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ToolSettings(BaseSettings):
    """Which Host-owned tools are registered into the toolkit."""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_TOOL_", extra="ignore")

    ui_enabled: bool = True


__all__ = ["ToolSettings"]
