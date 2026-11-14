# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PathsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_", extra="ignore")

    home: Path = Path.home() / ".qwenpaw-data"

    @field_validator("home")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()
