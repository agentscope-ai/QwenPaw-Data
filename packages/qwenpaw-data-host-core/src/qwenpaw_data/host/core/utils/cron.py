# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger

from qwenpaw_data.host.core.api.models.cron import ScheduleSpec
from qwenpaw_data.host.core.utils.time import utcnow


def compute_next_run_at(
    schedule: ScheduleSpec, *, now: datetime | None = None
) -> datetime | None:
    now = now or utcnow()
    if schedule.type == "once":
        if schedule.run_at is None:
            raise ValueError("schedule.run_at is required when type=once")
        run_at = schedule.run_at.astimezone(timezone.utc)
        return run_at if run_at > now else None
    if schedule.cron is None:
        raise ValueError("schedule.cron is required when type=cron")
    minute, hour, day, month, dow = schedule.cron.split()
    nxt = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=dow,
        timezone=schedule.timezone,
    ).get_next_fire_time(None, now)
    if nxt is None:
        return None
    if nxt.tzinfo is None:
        return nxt.replace(tzinfo=timezone.utc)
    return nxt.astimezone(timezone.utc)
