# -*- coding: utf-8 -*-
from __future__ import annotations

import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from qwenpaw_data_auth.database import shared_database_url
from sqlalchemy.engine import make_url


def validate_legacy_store_mode() -> None:
    """Do not silently replace an explicitly selected legacy file store."""
    mode = os.environ.get("QWENPAW_DATA_STORE", "sqlite").strip().lower()
    if mode in {"", "sqlite"}:
        return
    if mode == "json":
        raise RuntimeError(
            "QWENPAW_DATA_STORE=json is not connected to the current Host runtime. "
            "Startup stopped before database initialization; existing JSON data "
            "has not been migrated. Keep using the previous compatible release "
            "for that data until JSON compatibility or an explicit migration is available."
        )
    raise RuntimeError(
        "Unsupported QWENPAW_DATA_STORE value. The current Host uses SQL storage; "
        "select its database with QWENPAW_DATA_DB_URL."
    )


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_DB_", extra="ignore", hide_input_in_errors=True
    )

    url: str = Field(default="", repr=False, validate_default=True)
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_recycle_seconds: int = Field(default=600, gt=0)

    @field_validator("url")
    @classmethod
    def _async_shared_url(cls, value: str) -> str:
        # Unset selects the control-plane database shared with Auth and Context.
        url = make_url(shared_database_url(value))
        backend = url.get_backend_name()
        if backend == "sqlite":
            url = url.set(drivername="sqlite+aiosqlite")
        elif backend == "postgresql":
            query = dict(url.query)
            if "sslmode" in query and "ssl" not in query:
                query["ssl"] = query.pop("sslmode")
            url = url.set(drivername="postgresql+asyncpg", query=query)
        return url.render_as_string(hide_password=False)
