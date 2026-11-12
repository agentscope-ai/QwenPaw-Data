# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def _ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


class CatalogSettings(BaseSettings):
    """``QWENPAW_DATA_CHANNELS`` / ``QWENPAW_DATA_PROVIDERS``, comma-separated."""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_", extra="ignore")

    channels: str = "dingtalk"
    providers: str = "dashscope,openai"

    @property
    def channel_ids(self) -> set[str]:
        return _ids(self.channels)

    @property
    def provider_ids(self) -> set[str]:
        return _ids(self.providers)
