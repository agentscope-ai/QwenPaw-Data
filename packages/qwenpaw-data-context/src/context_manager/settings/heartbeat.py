"""Deployment timing for in-flight work heartbeats and the orphan sweep.

Workers read these values when they start. A running process keeps the
snapshot from ``get_settings()`` until restart.

``interval_seconds`` is how often a live worker refreshes its row. Default 30
seconds, finite and greater than 0.

``stale_after_seconds`` is the age after which another worker may take the row.
Default 300 seconds. It must be greater than the refresh interval.

``sweep_interval_seconds`` is how often the process scans for abandoned rows.
Default 60 seconds, finite and greater than 0.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HeartbeatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CM_HEARTBEAT_", extra="ignore")

    interval_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    stale_after_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    sweep_interval_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _stale_window_covers_one_refresh(self) -> HeartbeatSettings:
        if self.stale_after_seconds <= self.interval_seconds:
            raise ValueError(
                "stale_after_seconds must be greater than interval_seconds"
            )
        return self
