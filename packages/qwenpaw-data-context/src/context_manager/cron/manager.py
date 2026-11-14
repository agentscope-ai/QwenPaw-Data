"""Schedule persisted CM tasks and own their local execution lifecycle."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..settings import get_settings
from ..settings.cron import CronSettings
from .repository import CronJobRepository
from .schedule import ScheduleSpec

if TYPE_CHECKING:
    from ..user import IngestAdapter

_LOGGER = logging.getLogger(__name__)


class CronManager:
    """Execute shared task plans using renewable, per-execution claims."""

    def __init__(
        self,
        ingest_adapter: IngestAdapter,
        *,
        settings: CronSettings | None = None,
    ) -> None:
        self.jobs = CronJobRepository()
        self._settings = settings or get_settings().cron
        self._handlers = {"user_ingest": ingest_adapter.ingest}
        self._started = False
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(self._settings.max_threads)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Seed the built-in task and load persisted execution deadlines."""
        if self.started or not self._settings.enabled:
            return
        schedule = ScheduleSpec(
            cron=f"{self._settings.minute} {self._settings.hour} * * *",
            timezone=self._settings.timezone,
        )
        await asyncio.to_thread(
            self.jobs.create,
            "user_ingest",
            "user_ingest",
            schedule,
        )
        self._started = True
        try:
            await self.refresh()
        except Exception:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        """Cancel timers and await cleanup of this process's active runs."""
        self._started = False
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        runs = list(self._runs.values())
        for run in runs:
            run.cancel()
        await asyncio.gather(*runs, return_exceptions=True)

    async def refresh(self) -> None:
        """Reload plans explicitly after repository edits on this instance."""
        if not self.started:
            return
        rows = await asyncio.to_thread(
            self.jobs.list_scheduled,
            tuple(self._handlers),
        )
        if not self.started:
            return
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for row in rows:
            if row["id"] not in self._runs:
                self._schedule(row)

    def _schedule(self, job: dict) -> None:
        timer = self._timers.pop(job["id"], None)
        if timer is not None:
            timer.cancel()
        if job["next_run_at"] is None:
            return
        now = datetime.now(UTC)
        due = datetime.fromisoformat(job["next_run_at"])
        if due is not None:
            self._timers[job["id"]] = asyncio.get_running_loop().call_later(
                max(0, (due - now).total_seconds()),
                self._launch,
                job["id"],
            )

    def _launch(self, job_id: str) -> None:
        self._timers.pop(job_id, None)
        task = asyncio.create_task(self._execute(job_id), name=f"cm-cron-{job_id}")
        self._runs[job_id] = task

    async def _execute(self, job_id: str) -> None:
        row = None
        try:
            async with self._slots:
                claimed = await asyncio.to_thread(
                    self.jobs.try_start, job_id, datetime.now(UTC)
                )
                if claimed is not None:
                    await self._run(claimed)
                row = await asyncio.to_thread(self.jobs.get, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("cron dispatch failed: %s", job_id)
            try:
                row = await asyncio.to_thread(self.jobs.get, job_id)
            except Exception:
                _LOGGER.exception("cron reschedule read failed: %s", job_id)
        finally:
            self._runs.pop(job_id, None)
            if self.started and row is not None:
                self._schedule(row)

    async def _run(self, job: dict) -> None:
        try:
            await self._handlers[job["handler"]](datetime.now(UTC))
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("cron job failed: %s", job["id"])
