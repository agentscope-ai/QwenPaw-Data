# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from qwenpaw_data_auth.config import local_master_secret

from qwenpaw_data.host.core.settings.paths import PathsSettings


def _checked_secret(secret: str) -> str:
    if len(secret) != 64:
        raise ValueError(
            "QWENPAW_DATA_PREFERENCES_MASTER_SECRET must be 64 hex characters"
        )
    try:
        bytes.fromhex(secret)
    except ValueError as exc:
        raise ValueError(
            "QWENPAW_DATA_PREFERENCES_MASTER_SECRET must be hex-encoded"
        ) from exc
    return secret


class SecuritySettings(BaseSettings):
    """Host-level crypto material (env-injected)."""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    env: str = Field(default="development", validation_alias="QWENPAW_DATA_ENV")
    preferences_master_secret: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "QWENPAW_DATA_PREFERENCES_MASTER_SECRET",
            "QWENPAW_DATA_PREFS_MASTER_SECRET",
        ),
    )

    @field_validator("preferences_master_secret")
    @classmethod
    def _validate_master_secret(cls, value: str) -> str:
        secret = value.strip()
        return _checked_secret(secret) if secret else ""

    def master_key(self) -> bytes:
        secret = self.preferences_master_secret
        if not secret:
            if self.env.strip().lower() in {"prod", "production"}:
                raise RuntimeError(
                    "QWENPAW_DATA_PREFERENCES_MASTER_SECRET must be set in production"
                )
            # Context reads the same per-home key, so both decrypt shared provider keys.
            secret = _checked_secret(local_master_secret(PathsSettings().home))
        return bytes.fromhex(secret)
