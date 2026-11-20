"""Bound work-unit admission and thread execution; resource owners manage lifecycle."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from functools import partial
from inspect import iscoroutinefunction

from anyio import CapacityLimiter, to_thread

from .settings.execution import PoolSettings

log = logging.getLogger(__name__)


async def drain(task):
    """Delay cancellation until actual work finishes, including repeated cancellation."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class _ExecutionAdmission:
    """Bound FIFO admission of work units without occupying worker threads.

    ``_active`` counts granted permits, including handoffs, up to ``_limit``.
    ``_waiters`` holds FIFO tickets for queued work units; ``_max_waiters`` and
    ``_queue_timeout`` bound queue size and wait duration. ``_closed`` rejects
    new work units; ``_idle`` signals that no admitted work units remain.
    All admission state belongs to one event loop.

    ``_acquire()`` grants or waits for a permit; ``_release()`` transfers it
    to the next waiter or reduces the active count. ``_slot()`` pairs these
    operations through success, failure and cancellation. ``stop_accepting()``
    rejects new and queued work units while admitted work units finish.
    """

    def __init__(
        self, name: str, *, limit: int, max_waiters: int, queue_timeout: float
    ):
        self._name = name
        self._limit = limit
        self._max_waiters = max_waiters
        self._queue_timeout = queue_timeout
        self._active = 0
        self._waiters: deque[asyncio.Future] = deque()
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def _acquire(self):
        if self._closed:
            log.warning("%s execution is closed", self._name)
            raise BlockingIOError(f"{self._name} execution is closed")
        if self._active < self._limit:
            self._active += 1
            self._idle.clear()
            return
        if len(self._waiters) >= self._max_waiters:
            log.warning("%s execution queue is full", self._name)
            raise BlockingIOError(f"{self._name} execution queue is full")
        ticket = asyncio.get_running_loop().create_future()
        self._waiters.append(ticket)
        try:
            async with asyncio.timeout(self._queue_timeout):
                await ticket
        except (TimeoutError, asyncio.CancelledError) as exc:
            if not ticket.cancelled() and ticket.exception() is None:
                # Cancellation can arrive after a permit has already been granted.
                self._release()
            if isinstance(exc, TimeoutError):
                log.warning("%s execution queue timed out", self._name)
                raise BlockingIOError(
                    f"{self._name} execution queue timed out"
                ) from exc
            raise
        finally:
            if ticket in self._waiters:
                self._waiters.remove(ticket)

    def _release(self):
        while self._waiters:
            ticket = self._waiters.popleft()
            if not ticket.done():
                ticket.set_result(None)
                return
        self._active -= 1
        if self._active == 0:
            self._idle.set()

    @asynccontextmanager
    async def _slot(self):
        await self._acquire()
        try:
            if self._closed:
                log.warning("%s execution is closed", self._name)
                raise BlockingIOError(f"{self._name} execution is closed")
            yield
        finally:
            self._release()

    def stop_accepting(self):
        self._closed = True
        while self._waiters:
            ticket = self._waiters.popleft()
            if not ticket.done():
                log.warning("%s execution is closed", self._name)
                ticket.set_exception(
                    BlockingIOError(f"{self._name} execution is closed")
                )


class BoundedExecutor:
    """Bound work-unit admission and synchronous execution in AnyIO threads.

    A work unit is one function call submitted through ``run()``, including
    its synchronous steps and asynchronous waits until the call finishes.
    ``_admission`` limits admitted work units; ``_workers`` limits synchronous
    steps executing in threads.

    ``run()`` admits once, then awaits an async function on the owner loop or
    offloads a sync function. ``run_sync()`` offloads a blocking phase without
    re-entering admission or creating another work unit. Callers must already
    be admitted and await their child tasks before the work unit ends.
    ``drain()`` delays cancellation until synchronous work actually ends,
    keeping its resources alive meanwhile.
    """

    def __init__(self, name: str, settings: PoolSettings):
        self._name = name
        self._admission = _ExecutionAdmission(
            name,
            limit=settings.workers,
            max_waiters=settings.max_waiters,
            queue_timeout=settings.queue_timeout_seconds,
        )
        self._workers = CapacityLimiter(settings.workers)

    async def run(self, operation, /, *args, **kwargs):
        """Submit one synchronous or asynchronous work unit for FIFO admission."""
        async with self._admission._slot():
            if iscoroutinefunction(operation):
                return await operation(*args, **kwargs)
            return await self.run_sync(operation, *args, **kwargs)

    async def run_sync(self, operation, /, *args, **kwargs):
        """Execute a synchronous step within an admitted work unit without re-admission."""
        work = asyncio.create_task(
            to_thread.run_sync(
                partial(operation, *args, **kwargs),
                limiter=self._workers,
            )
        )
        # Keep the work unit and its resources alive until the worker finishes.
        return await drain(work)

    def stop_accepting(self):
        self._admission.stop_accepting()

    def close(self):
        """Stop admission outside serving; AnyIO owns the reusable worker threads."""
        self.stop_accepting()

    async def aclose(self):
        self.stop_accepting()
        await drain(asyncio.create_task(self._admission._idle.wait()))
