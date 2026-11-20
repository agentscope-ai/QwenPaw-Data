"""Serialize recurring and one-shot schedules and calculate their next fire."""

from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, model_validator


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["cron", "once"] = "cron"
    cron: str | None = None
    run_at: datetime | None = None
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone: {self.timezone}") from None
        if self.type == "once":
            if self.run_at is None or self.run_at.tzinfo is None:
                raise ValueError("once schedules require an aware run_at")
        else:
            self._trigger()
        return self

    def _trigger(self) -> CronTrigger:
        parts = (self.cron or "").split()
        if len(parts) != 5:
            raise ValueError("cron must have 5 fields: min hour dom month dow")
        # Crontab numbers Sunday as 0/7; APScheduler numbers Monday as 0.
        dow = self._weekdays(parts[4])
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=dow,
            timezone=self.timezone,
        )

    @staticmethod
    def _weekdays(expression: str) -> str:
        names = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
        normalized = []
        for part in expression.split(","):
            if any(character.isalpha() for character in part):
                normalized.append(part)
                continue
            span, _, stride = part.partition("/")
            step = int(stride) if stride else 1
            if span == "*":
                start, end = 0, 6
            elif "-" in span:
                start, end = map(int, span.split("-"))
            else:
                start = int(span)
                end = 7 if stride else start
            if not 0 <= start <= end <= 7 or step < 1:
                raise ValueError(
                    "cron weekday must be between 0 and 7 with a positive step"
                )
            normalized.extend(names[day] for day in range(start, end + 1, step))
        return ",".join(normalized)

    def next_run(self, after: datetime) -> datetime | None:
        """Return a UTC fire strictly after the supplied instant."""
        if after.tzinfo is None:
            raise ValueError("after must be timezone-aware")
        if self.type == "once":
            return self.run_at.astimezone(UTC) if self.run_at > after else None
        next_fire = self._trigger().get_next_fire_time(
            None,
            after + timedelta(microseconds=1),
        )
        return next_fire.astimezone(UTC) if next_fire is not None else None
