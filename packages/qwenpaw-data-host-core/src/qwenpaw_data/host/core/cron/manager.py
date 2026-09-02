# -*- coding: utf-8 -*-
"""Cron scheduler + console agent execution."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from qwenpaw_data.host.core.api.models.cron import ScheduleSpec
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.store.protocols import (
    ChatEventStore,
    ChatStore,
    CronStore,
    SessionStore,
)

logger = logging.getLogger(__name__)


class CronManager:
    """App-owned scheduler that opens console chats on schedule."""

    def __init__(
        self,
        *,
        cron: CronStore,
        sessions: SessionStore,
        chats: ChatStore,
        events: ChatEventStore,
        hosts: QwenPawDataHostRegistry,
    ) -> None:
        self._cron = cron
        self._sessions = sessions
        self._chats = chats
        self._events = events
        self._hosts = hosts
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    async def start(self) -> None:
        self._scheduler.start()
        for job in await self._cron.list_all():
            try:
                self.sync(job)
            except Exception:
                logger.exception("skip invalid cron job: %s", job.get("id"))

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync(self, job: dict[str, Any]) -> None:
        schedule = ScheduleSpec.model_validate(job["schedule"])
        self._scheduler.add_job(
            self._on_schedule,
            trigger=self._trigger(schedule),
            id=job["id"],
            args=[job["id"]],
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        if not job["enabled"]:
            self._scheduler.pause_job(job["id"])

    def remove(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    async def run(self, job: dict[str, Any]) -> None:
        try:
            await self._run_console(job)
        except Exception:
            logger.exception("cron run failed: %s", job["id"])

    def _trigger(self, schedule: ScheduleSpec) -> CronTrigger | DateTrigger:
        if schedule.type == "once":
            assert schedule.run_at is not None
            return DateTrigger(run_date=schedule.run_at, timezone=schedule.timezone)
        assert schedule.cron is not None
        minute, hour, day, month, dow = schedule.cron.split()
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=schedule.timezone,
        )

    async def _on_schedule(self, job_id: str) -> None:
        try:
            job = await self._cron.get_by_id(job_id)
        except LookupError:
            return
        if job["enabled"]:
            await self.run(job)

    async def _run_console(self, job: dict[str, Any]) -> None:
        identity = Identity(user_id=job["user_id"])
        datasource_id = (job.get("datasource_id") or "").strip()
        if not datasource_id:
            raise ValueError("datasource_id is required for agent cron")
        wanted_id = (job.get("session_id") or "").strip() or None
        if wanted_id:
            session = await self._sessions.get(wanted_id)
        else:
            session = Session.create(
                identity=identity,
                title=(job["name"] or "定时任务")[:80],
                datasource_id=datasource_id,
            )
        chat = session.open_chat(
            text=job["message"],
            datasource_id=datasource_id,
            has_active_chat=await self._sessions.has_active_chat(session.id),
        )
        if wanted_id:
            await self._sessions.save(session)
        else:
            await self._sessions.add(session)
        await self._chats.add(chat)
        await ChatRuntime(
            chats=self._chats,
            events=self._events,
            hosts=self._hosts,
        ).run(chat.id, identity=identity)
