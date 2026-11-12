"""Own explicitly submitted embedding workers and shared SQL job state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from ..model.usage import bind_usage_context
from ..settings import get_settings
from ..settings.embedding import EmbeddingSettings
from .rebuild import ProgressCallback
from .repository import EmbeddingJobRepository, JobOwner, rebuild_rid

RebuildWork = Callable[[ProgressCallback, str, str, str | None], Awaitable[int]]
_log = logging.getLogger(__name__)


class _LeaseLost(RuntimeError):
    """The current execution may no longer update this job."""


class EmbeddingJobManager:
    """Manage execution leases and shutdown; SQL owns durable state."""

    def __init__(self, *, settings: EmbeddingSettings | None = None) -> None:
        self._repository = EmbeddingJobRepository()
        self._settings = settings or get_settings().embedding
        self._tasks: dict[asyncio.Task, tuple[JobOwner, str, str]] = {}
        self._closed = False

    async def _db(self, operation, *args, **kwargs):
        """Drain in-flight SQL before cancellation releases a lease or closes the engine."""
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def get(self, owner: JobOwner, job_id: str) -> dict | None:
        return await self._db(self._repository.get, owner, job_id)

    async def latest(self, owner: JobOwner) -> dict | None:
        return await self._db(self._repository.latest, owner)

    async def submit(
        self,
        owner: JobOwner,
        config_snapshot: dict,
        work: RebuildWork,
        *,
        created_by: str,
        job_id: str | None = None,
    ) -> str:
        if self._closed:
            raise RuntimeError("embedding job manager is closed")
        now = datetime.now(UTC).isoformat()
        retry = job_id is not None
        job_id = job_id or uuid4().hex
        resume_count, previous_rid = await self._db(
            self._repository.reserve,
            {
                "tenant_id": owner[0],
                "workspace_id": owner[1],
                "job_id": job_id,
                "created_by": created_by,
                "created_at": now,
                "attempted_at": now,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "error": None,
                "progress": {
                    "phase": "pending",
                    "current_label": "",
                    "labels_done": 0,
                    "labels_total": 0,
                },
                "config_snapshot": {
                    name: config_snapshot[name] for name in ("model", "base_url", "dim")
                },
            },
            retry=retry,
        )
        self._launch(
            owner,
            job_id,
            resume_count,
            work,
            rebuild_rid(job_id, now),
            previous_rid,
        )
        return job_id

    def adopt(
        self,
        owner: JobOwner,
        job_id: str,
        resume_count: int,
        work: RebuildWork,
        rid: str,
        previous_rid: str | None = None,
    ) -> None:
        """Run a job that startup already claimed. Does not claim it again."""
        self._launch(owner, job_id, resume_count, work, rid, previous_rid)

    def _launch(
        self,
        owner: JobOwner,
        job_id: str,
        token: int,
        work: RebuildWork,
        rid: str,
        previous_rid: str | None,
    ) -> None:
        if self._closed:
            return  # The reservation expires and becomes available for an explicit retry.
        task = asyncio.create_task(
            self._run(owner, job_id, token, work, rid, previous_rid)
        )
        self._tasks[task] = (owner, job_id, token)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.pop(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _log.error("embedding job state could not be persisted", exc_info=error)

    @asynccontextmanager
    async def _lease(self, owner: JobOwner, job_id: str, resume_count: int):
        worker = asyncio.current_task()
        lost = False

        async def renew():
            nonlocal lost
            while True:
                await asyncio.sleep(get_settings().heartbeat.interval_seconds)
                try:
                    owned = await self._db(
                        self._repository.heartbeat,
                        owner,
                        job_id,
                        resume_count,
                    )
                except Exception:
                    _log.exception("embedding job heartbeat failed")
                    continue
                if not owned:
                    lost = True
                    worker.cancel()
                    return

        heartbeat = asyncio.create_task(renew())
        try:
            yield
            if lost:
                raise _LeaseLost
        except asyncio.CancelledError:
            if lost:
                raise _LeaseLost from None
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _run(
        self,
        owner: JobOwner,
        job_id: str,
        token: int,
        work: RebuildWork,
        rid: str,
        previous_rid: str | None,
    ) -> None:
        status, error = "failed", None
        progress = {
            "phase": "pending",
            "current_label": "",
            "labels_done": 0,
            "labels_total": 0,
        }
        owned = True

        async def report(value: dict) -> None:
            nonlocal progress
            progress = value
            if not await self._db(
                self._repository.update_progress, owner, job_id, token, value
            ):
                raise _LeaseLost

        try:
            async with self._lease(owner, job_id, token):
                if not await self._db(
                    self._repository.mark_running, owner, job_id, token
                ):
                    raise _LeaseLost
                with bind_usage_context(
                    source="向量索引维护",
                    source_step=f"vector_index_rebuild#{job_id}",
                    session_id="",
                    chat_id="",
                    probe=False,
                ):
                    await work(report, job_id, rid, previous_rid)
                status = "success"
                progress.update(phase="done", current_label="")
        except _LeaseLost:
            owned = False
        except asyncio.CancelledError:
            error = "Embedding rebuild was interrupted; retry to rebuild from the beginning."
            progress["phase"] = "failed"
        except Exception as exc:
            _log.warning("embedding job %s failed (%s)", job_id, type(exc).__name__)
            status = "failed"
            progress["phase"] = "failed"
            error = "Embedding rebuild failed; check the original initiator's model configuration and retry."
        finally:
            if owned:
                await self._db(
                    self._repository.finish,
                    owner,
                    job_id,
                    token,
                    status=status,
                    progress=progress,
                    error=error,
                )

    async def aclose(self) -> None:
        """Drain local workers before shared resources close."""
        self._closed = True
        tasks = dict(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task, (owner, job_id, token) in tasks.items():
            if task.cancelled():
                # A task cancelled before its first step cannot run its own finalizer.
                await self._db(
                    self._repository.finish,
                    owner,
                    job_id,
                    token,
                    status="failed",
                    progress={
                        "phase": "failed",
                        "current_label": "",
                        "labels_done": 0,
                        "labels_total": 0,
                    },
                    error="Embedding rebuild was interrupted; retry to rebuild from the beginning.",
                )
