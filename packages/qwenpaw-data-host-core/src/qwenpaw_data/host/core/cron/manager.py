# -*- coding: utf-8 -*-
"""Cron poller + console agent execution."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.channels.manager import ChannelManager
from qwenpaw_data.host.core.channels.schema import ChannelType
from qwenpaw_data.host.core.db.tables import CronJobRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.cron import CronJobRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.auth import generate_temporary_token
from qwenpaw_data.host.core.utils.cron import check_session_exists
from qwenpaw_data.host.core.utils.time import utcnow

logger = logging.getLogger(__name__)

_CONSOLE = ChannelType.CONSOLE.value


class CronManager:
    _running: CronManager | None = None

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channel_manager: ChannelManager,
    ) -> None:
        self._session_factory = session_factory
        self._channel_manager = channel_manager
        self._task: asyncio.Task[None] | None = None
        self._runs: set[asyncio.Task[None]] = set()

    @classmethod
    def require(cls) -> CronManager:
        if cls._running is None:
            raise RuntimeError("CronManager has not started")
        return cls._running

    @property
    def channel_manager(self) -> ChannelManager:
        return self._channel_manager

    async def start(self) -> None:
        if CronManager._running is self:
            return
        if CronManager._running is not None:
            raise RuntimeError("CronManager is already running")
        async with self._session_factory() as db:
            await CronJobRepository.fill_missing_next_run_at(db)
            await db.commit()
        CronManager._running = self
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if CronManager._running is not self:
            return
        task = self._task
        self._task = None
        CronManager._running = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        runs = list(self._runs)
        for run in runs:
            run.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)

    async def _loop(self) -> None:
        interval = get_settings().cron.poll_seconds
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("cron tick failed")
            await asyncio.sleep(interval)

    async def tick(self) -> None:
        async with self._session_factory() as db:
            await CronJobRepository.clear_expired(db)
            await db.commit()
        async with self._session_factory() as db:
            jobs = await CronJobRepository.list_ready_to_run(db)
        max_runs = get_settings().cron.max_threads
        for job in jobs:
            if len(self._runs) >= max_runs:
                break
            if await self._session_busy(job):
                continue
            started = await self._try_start(job)
            if started is None:
                continue
            task = asyncio.create_task(
                self.run(job, started), name=f"cron-run-{job['id']}"
            )
            self._runs.add(task)
            task.add_done_callback(self._runs.discard)

    async def _session_busy(self, job: dict[str, Any]) -> bool:
        session_id = (job.get("session_id") or "").strip()
        if not session_id:
            return False
        identity = Identity(
            user_id=job["user_id"],
            tenant_id=job["tenant_id"],
            workspace_id=job["workspace_id"],
        )
        async with self._session_factory() as db:
            return await SessionRepository(db, identity).has_active_chat(session_id)

    async def _try_start(self, job: dict[str, Any]) -> datetime | None:
        async with self._session_factory() as db:
            started = await CronJobRepository.try_start(db, job["id"])
            if started is None:
                return None
            await db.commit()
            return started

    async def _refresh_heartbeat(self, job_id: str, running_at: datetime) -> None:
        interval = get_settings().cron.running_ttl_seconds / 3
        while True:
            await asyncio.sleep(interval)
            async with self._session_factory() as db:
                result = await db.execute(
                    update(CronJobRow)
                    .where(
                        CronJobRow.id == job_id,
                        CronJobRow.running_at == running_at,
                    )
                    .values(heartbeat_at=utcnow())
                )
                await db.commit()
            if result.rowcount != 1:
                return

    async def run(self, job: dict[str, Any], running_at: datetime) -> None:
        beat = asyncio.create_task(
            self._refresh_heartbeat(job["id"], running_at),
            name=f"cron-heartbeat-{job['id']}",
        )
        try:
            channel = (job.get("channel") or "").strip()
            if channel == _CONSOLE:
                await self._run_console(job)
            else:
                await self._channel_manager.run_cron_job(job)
        except Exception:
            logger.exception("cron run failed: %s", job["id"])
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("cron heartbeat failed: %s", job["id"])
            async with self._session_factory() as db:
                await CronJobRepository.clear_running(db, job["id"], running_at)
                await db.commit()

    async def _run_console(self, job: dict[str, Any]) -> None:
        identity = Identity(
            user_id=job["user_id"],
            tenant_id=job["tenant_id"],
            workspace_id=job["workspace_id"],
        )
        datasource_id = (job.get("datasource_id") or "").strip()
        if not datasource_id:
            raise ValueError("datasource_id is required for agent cron")
        wanted_id = (job.get("session_id") or "").strip() or None
        async with self._session_factory() as db:
            session_repo = SessionRepository(db, identity)
            if wanted_id:
                await check_session_exists(db, identity, wanted_id, _CONSOLE)
                session = await session_repo.get(wanted_id)
            else:
                session = Session.create(
                    identity=identity,
                    title=(job["name"] or "定时任务")[:80],
                    channel=_CONSOLE,
                    datasource_id=datasource_id,
                )
            chat = session.open_chat(
                text=job["message"],
                datasource_id=datasource_id,
                has_active_chat=await session_repo.has_active_chat(session.id),
                enable_clarification=False,
            )
            if wanted_id:
                await session_repo.save(session)
            else:
                await session_repo.add(session)
            await ChatRepository(db, identity).add(chat)
            await db.commit()
            chat_id = chat.id
        token = await asyncio.to_thread(generate_temporary_token, identity)
        await ChatRuntime(self._session_factory, chat_id).run(
            identity=identity, access_token=token
        )
