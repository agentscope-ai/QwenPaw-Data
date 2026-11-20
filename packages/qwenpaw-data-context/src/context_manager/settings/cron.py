"""Configure cron workers and the initial User Graph task schedule."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class CronSettings:
    enabled: bool = True
    max_threads: int = 8
    running_ttl_seconds: float = 90
    hour: int = 23
    minute: int = 0
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if self.max_threads < 1 or self.running_ttl_seconds <= 0:
            raise ValueError("cron concurrency and lease duration must be positive")
        if self.enabled:
            if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
                raise ValueError("cron hour and minute must be within the daily clock")
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError:
                raise ValueError(f"unknown timezone: {self.timezone}") from None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> CronSettings:
        values = os.environ if environment is None else environment
        enabled = values.get("CM_SCHEDULER_ENABLED", "1").strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
        }
        hour = int(values.get("CM_SCHEDULER_HOUR", "23"))
        minute = int(values.get("CM_SCHEDULER_MINUTE", "0"))
        timezone = (
            values.get("CM_SCHEDULER_TIMEZONE", "Asia/Shanghai").strip()
            or "Asia/Shanghai"
        )
        return cls(
            enabled=enabled,
            hour=hour,
            minute=minute,
            timezone=timezone,
            max_threads=int(values.get("CM_CRON_MAX_THREADS", "8")),
            running_ttl_seconds=float(values.get("CM_CRON_RUNNING_TTL_SECONDS", "90")),
        )
