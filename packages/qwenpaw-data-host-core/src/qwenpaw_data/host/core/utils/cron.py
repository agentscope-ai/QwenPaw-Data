# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.models.cron import ScheduleSpec
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.utils.time import utcnow


async def check_session_exists(
    db: AsyncSession,
    identity: Identity,
    session_id: str | None,
    channel: str,
) -> None:
    if not session_id:
        return
    try:
        session = await SessionRepository(db, identity).get(session_id)
    except LookupError:
        raise ValueError("session not found") from None
    if session.identity != identity:
        raise ValueError("session does not belong to this identity")
    if session.channel != channel:
        raise ValueError("session channel does not match job channel")


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
