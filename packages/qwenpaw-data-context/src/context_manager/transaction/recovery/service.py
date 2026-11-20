"""Expose recoverable runs and resume abandoned stacks without business adapters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from uuid import uuid4

from ...auth import ScopeBinding
from ...executor import BoundedExecutor, drain
from ...settings import get_settings
from ...store import StoreError
from ...utils.validation import require_trimmed_text
from .repository import RecoveryRunRepository
from .session import RecoveryStackSession
from .stack import RecoveryStack

log = logging.getLogger(__name__)
_TASK_KEY = ("tenant_id", "workspace_id", "task_type", "task_id")


class RecoveryService:
    """Own one process's active runs and bounded background recovery scans.

    Runtime supplies a factory bound to trusted target resolution and system
    authorization. Neither forward callers nor background recovery assemble
    Session dependencies or dispatch recovery by business task type.
    """

    def __init__(
        self,
        runs: RecoveryRunRepository,
        executor: BoundedExecutor,
        stack_for: Callable[[Mapping[str, object]], RecoveryStack],
    ) -> None:
        self._runs = runs
        self._executor = executor
        self._stack_for = stack_for
        self._active: dict[tuple[str, ...], str] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None
        self._closed = False

    @asynccontextmanager
    async def run(
        self,
        scope: ScopeBinding,
        *,
        task_type: str,
        task_id: str | None = None,
        rid: str | None = None,
        previous_rid: str | None = None,
    ) -> AsyncIterator[RecoveryStackSession]:
        if self._closed:
            raise RuntimeError("recovery service is closed")
        identity = {
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            "task_type": require_trimmed_text(task_type, "task_type"),
            "task_id": require_trimmed_text(
                task_id if task_id is not None else uuid4().hex, "task_id"
            ),
            "rid": require_trimmed_text(rid if rid is not None else uuid4().hex, "rid"),
        }
        key = tuple(identity[name] for name in _TASK_KEY)
        if key in self._active:
            raise StoreError(
                "recovery task is active",
                operation="recovery",
                code="recovery_task_busy",
            )
        self._active[key] = "execution"
        self._idle.clear()
        try:
            await self.require_ready(scope)
            stack = await self._executor.run(self._stack_for, identity)
            async with RecoveryStackSession(
                stack,
                self._runs,
                self._executor,
                identity,
                scope,
                previous_rid=previous_rid,
            ) as session:
                yield session
        finally:
            # Executor and Session exit drain in-flight work before this release.
            del self._active[key]
            if not self._active:
                self._idle.set()

    async def require_ready(self, scope: ScopeBinding) -> None:
        if await self._executor.run(
            self._runs.has_pending, scope.tenant_id, scope.workspace_id
        ):
            raise StoreError(
                "this workspace has an unfinished recovery run",
                operation="recovery",
                code="recovery_workspace_busy",
            )

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("recovery service is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._maintain())

    async def aclose(self) -> None:
        self._closed = True
        pending = [self._idle.wait()]
        if self._task is not None:
            self._task.cancel()
            pending.append(self._task)
        # Drain both the scanner and admitted runs even if shutdown is cancelled.
        await drain(asyncio.gather(*pending, return_exceptions=True))
        self._task = None

    async def _maintain(self) -> None:
        while True:
            try:
                await self.recover_pending()
            except Exception:
                log.exception("recovery scan failed; registrations remain pending")
            await asyncio.sleep(get_settings().heartbeat.interval_seconds)

    async def recover_pending(self) -> None:
        """Visit each pending registration once; isolate failures, not cancellation."""
        after = None
        while not self._closed:
            rows = await self._executor.run(self._runs.discover, after=after)
            if not rows:
                return
            for row in rows:
                if self._closed:
                    return
                key = tuple(row[name] for name in _TASK_KEY)
                if key in self._active:
                    continue
                self._active[key] = "recovery"
                self._idle.clear()
                try:
                    stack = await self._executor.run(self._stack_for, row)
                    session = RecoveryStackSession(
                        stack, self._runs, self._executor, row, None
                    )
                    await session.recover()
                except Exception:
                    log.exception("recovery failed for task %s", key)
                finally:
                    del self._active[key]
                    if not self._active:
                        self._idle.set()
            after = tuple(rows[-1][name] for name in _TASK_KEY)
