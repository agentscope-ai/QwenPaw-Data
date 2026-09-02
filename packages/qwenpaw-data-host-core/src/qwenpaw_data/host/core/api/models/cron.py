# -*- coding: utf-8 -*-
"""Cron API request models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import model_validator

from qwenpaw_data.host.core.api.models.common import ApiModel

_DAY_OF_WEEK = {
    "0": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}


def _normalize_dow(field: str) -> str:
    if field == "*" or field.isalpha() or ("-" in field and not field[0].isdigit()):
        return field
    return ",".join(_DAY_OF_WEEK.get(p, p) for p in field.split(","))


class ScheduleSpec(ApiModel):
    """When to run: recurring cron expression, or a one-shot run_at time."""

    type: Literal["cron", "once"] = "cron"
    cron: str | None = None
    run_at: datetime | None = None
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def _validate(self) -> ScheduleSpec:
        if self.type == "cron":
            parts = [p for p in (self.cron or "").split() if p]
            if len(parts) != 5:
                raise ValueError("cron must have 5 fields: min hour dom month dow")
            parts[4] = _normalize_dow(parts[4])
            self.cron = " ".join(parts)
            self.run_at = None
            return self
        if self.run_at is None:
            raise ValueError("schedule.run_at is required when type=once")
        self.cron = None
        return self


class CronJobWrite(ApiModel):
    name: str
    enabled: bool = True
    message: str
    datasource_id: str
    channel: str = "console"
    target_external_key: str | None = None
    session_id: str | None = None
    schedule: ScheduleSpec

    @model_validator(mode="after")
    def _trim(self) -> CronJobWrite:
        self.name = self.name.strip()
        self.message = self.message.strip()
        self.datasource_id = self.datasource_id.strip()
        self.channel = (self.channel or "console").strip() or "console"
        tek = (self.target_external_key or "").strip()
        self.target_external_key = tek or None
        sid = (self.session_id or "").strip()
        self.session_id = sid or None
        if not self.name:
            raise ValueError("name is required")
        if not self.message:
            raise ValueError("message is required")
        if not self.datasource_id:
            raise ValueError("datasource_id is required")
        if self.channel != "console" and not self.target_external_key:
            raise ValueError("target_external_key is required for IM channel jobs")
        return self
