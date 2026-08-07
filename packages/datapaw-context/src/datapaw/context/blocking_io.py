"""Bounded execution for synchronous work called from async request handlers.

The governor deliberately owns dedicated executors instead of relying on
``asyncio.to_thread``.  This gives each resource class an explicit concurrency
limit, a finite admission queue, and observable overload behaviour.  A timeout
only stops waiting for the result; Python cannot safely terminate a running
thread, so the worker slot remains occupied until the callable actually exits.
Callers must still configure hard timeouts on Neo4j, HTTP, SQL, and other
blocking clients.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, Callable, TypeVar


log = logging.getLogger("datapaw.context.blocking_io")

T = TypeVar("T")


class BlockingPool(StrEnum):
    GRAPH = "graph"
    FILE = "file"
    NETWORK = "network"
    SQL = "sql"


class BlockingIOError(RuntimeError):
    """Base class for governed blocking-I/O failures."""

    def __init__(self, pool: BlockingPool, operation: str, message: str) -> None:
        super().__init__(message)
        self.pool = pool
        self.operation = operation


class BlockingIOOverloaded(BlockingIOError):
    """Raised when a pool cannot admit work within its queue deadline."""


class BlockingIOTimeout(BlockingIOError):
    """Raised when waiting for an admitted operation exceeds its deadline."""


@dataclass(frozen=True)
class BlockingPoolConfig:
    max_workers: int
    max_queue: int
    queue_timeout_seconds: float
    operation_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if self.max_queue < 0:
            raise ValueError("max_queue must be >= 0")
        if self.queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be > 0")
        if self.operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be > 0")


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.001, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def blocking_pool_configs_from_env() -> dict[BlockingPool, BlockingPoolConfig]:
    """Build per-resource pool settings from environment variables."""

    defaults = {
        BlockingPool.GRAPH: (8, 32, 1.0, 60.0),
        BlockingPool.FILE: (4, 8, 1.0, 60.0),
        BlockingPool.NETWORK: (8, 16, 1.0, 30.0),
        BlockingPool.SQL: (4, 16, 1.0, 60.0),
    }
    configs: dict[BlockingPool, BlockingPoolConfig] = {}
    for pool, (workers, queue, queue_timeout, operation_timeout) in defaults.items():
        prefix = f"DATAPAW_BLOCKING_{pool.value.upper()}"
        configs[pool] = BlockingPoolConfig(
            max_workers=_positive_int_env(f"{prefix}_WORKERS", workers),
            max_queue=_positive_int_env(
                f"{prefix}_MAX_QUEUE", queue, minimum=0,
            ),
            queue_timeout_seconds=_positive_float_env(
                f"{prefix}_QUEUE_TIMEOUT_SECONDS", queue_timeout,
            ),
            operation_timeout_seconds=_positive_float_env(
                f"{prefix}_TIMEOUT_SECONDS", operation_timeout,
            ),
        )
    return configs


@dataclass
class _PoolMetrics:
    admitted: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    rejected: int = 0
    timed_out: int = 0
    cancelled: int = 0
    active: int = 0
    queued: int = 0
    queue_wait_ms_total: float = 0.0
    run_ms_total: float = 0.0


class _GovernedPool:
    def __init__(self, name: BlockingPool, config: BlockingPoolConfig) -> None:
        self.name = name
        self.config = config
        self.executor = ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix=f"datapaw-{name.value}",
        )
        # capacity bounds active + admitted-but-waiting work.  A callable is
        # submitted only after a worker token is acquired, so the executor's
        # own unbounded queue is never used as an application queue.
        self.capacity = asyncio.Semaphore(config.max_workers + config.max_queue)
        self.workers = asyncio.Semaphore(config.max_workers)
        self.metrics = _PoolMetrics()
        self.futures: set[asyncio.Future[Any]] = set()
        self.closed = False

    async def _acquire(
        self,
        semaphore: asyncio.Semaphore,
        deadline: float,
    ) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(semaphore.acquire(), timeout=remaining)

    async def run(
        self,
        operation: str,
        func: Callable[..., T],
        *args: Any,
        queue_timeout_seconds: float | None = None,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        if self.closed:
            raise BlockingIOOverloaded(
                self.name,
                operation,
                f"blocking pool {self.name.value!r} is shutting down",
            )

        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        queue_timeout = (
            queue_timeout_seconds
            if queue_timeout_seconds is not None
            else self.config.queue_timeout_seconds
        )
        if queue_timeout <= 0:
            raise ValueError("queue_timeout_seconds must be > 0")
        operation_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.config.operation_timeout_seconds
        )
        if operation_timeout <= 0:
            raise ValueError("timeout_seconds must be > 0")
        deadline = queued_at + queue_timeout
        capacity_acquired = False
        worker_acquired = False

        # Do not let callers form an unbounded coroutine queue in front of the
        # bounded application queue. Semaphore acquire does not suspend when a
        # token is available, so locked()+acquire() is atomic on this event
        # loop turn.
        if self.capacity.locked():
            self.metrics.rejected += 1
            raise BlockingIOOverloaded(
                self.name,
                operation,
                f"blocking pool {self.name.value!r} is saturated",
            )

        try:
            await self.capacity.acquire()
            capacity_acquired = True
            self.metrics.admitted += 1
            self.metrics.queued += 1
            await self._acquire(self.workers, deadline)
            worker_acquired = True
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self.metrics.rejected += 1
            if capacity_acquired:
                self.metrics.queued -= 1
                self.capacity.release()
            raise BlockingIOOverloaded(
                self.name,
                operation,
                f"blocking pool {self.name.value!r} is saturated",
            ) from exc
        except asyncio.CancelledError:
            self.metrics.cancelled += 1
            if capacity_acquired:
                self.metrics.queued -= 1
                self.capacity.release()
            raise
        except BaseException:
            if capacity_acquired:
                self.metrics.queued -= 1
                self.capacity.release()
            raise

        if self.closed:
            self.metrics.queued -= 1
            self.metrics.rejected += 1
            self.workers.release()
            self.capacity.release()
            raise BlockingIOOverloaded(
                self.name,
                operation,
                f"blocking pool {self.name.value!r} is shutting down",
            )

        self.metrics.queued -= 1
        self.metrics.started += 1
        self.metrics.active += 1
        self.metrics.queue_wait_ms_total += (loop.time() - queued_at) * 1000
        started_at = loop.time()

        # Match ``asyncio.to_thread`` semantics: request-scoped values such as
        # the selected Neo4j logical database must cross the thread boundary.
        context = contextvars.copy_context()
        call = partial(context.run, func, *args, **kwargs)
        try:
            future = loop.run_in_executor(self.executor, call)
        except BaseException:
            self.metrics.active -= 1
            self.metrics.completed += 1
            self.metrics.failed += 1
            self.workers.release()
            self.capacity.release()
            raise
        self.futures.add(future)

        def _release(done: asyncio.Future[Any]) -> None:
            self.futures.discard(done)
            self.metrics.active -= 1
            self.metrics.completed += 1
            self.metrics.run_ms_total += (loop.time() - started_at) * 1000
            try:
                exception = done.exception()
            except asyncio.CancelledError:
                exception = None
            if exception is not None:
                self.metrics.failed += 1
            if worker_acquired:
                self.workers.release()
            if capacity_acquired:
                self.capacity.release()

        future.add_done_callback(_release)
        try:
            # Shielding is intentional: timing out/cancelling the request must
            # not release capacity while its thread is still mutating state.
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=operation_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self.metrics.timed_out += 1
            raise BlockingIOTimeout(
                self.name,
                operation,
                f"blocking operation {operation!r} exceeded {operation_timeout:g}s",
            ) from exc
        except asyncio.CancelledError:
            self.metrics.cancelled += 1
            raise

    def snapshot(self) -> dict[str, int | float]:
        metrics = self.metrics
        return {
            "max_workers": self.config.max_workers,
            "max_queue": self.config.max_queue,
            "queue_timeout_seconds": self.config.queue_timeout_seconds,
            "operation_timeout_seconds": self.config.operation_timeout_seconds,
            "active": metrics.active,
            "queued": metrics.queued,
            "admitted": metrics.admitted,
            "started": metrics.started,
            "completed": metrics.completed,
            "failed": metrics.failed,
            "rejected": metrics.rejected,
            "timed_out": metrics.timed_out,
            "cancelled": metrics.cancelled,
            "queue_wait_ms_total": round(metrics.queue_wait_ms_total, 3),
            "run_ms_total": round(metrics.run_ms_total, 3),
        }

    async def aclose(self, timeout_seconds: float) -> None:
        self.closed = True
        pending = tuple(self.futures)
        unfinished: set[asyncio.Future[Any]] = set()
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=timeout_seconds)
        self.executor.shutdown(wait=not unfinished, cancel_futures=True)
        if unfinished:
            log.warning(
                "blocking pool %s closed with %d running operation(s)",
                self.name.value,
                len(unfinished),
            )


class BlockingIOGovernor:
    """Own bounded executors for blocking work issued by async handlers."""

    def __init__(
        self,
        configs: dict[BlockingPool, BlockingPoolConfig] | None = None,
        *,
        shutdown_timeout_seconds: float | None = None,
    ) -> None:
        resolved = configs or blocking_pool_configs_from_env()
        missing = set(BlockingPool) - set(resolved)
        if missing:
            raise ValueError(f"missing blocking pool configs: {sorted(missing)}")
        self._pools = {
            name: _GovernedPool(name, resolved[name]) for name in BlockingPool
        }
        self.shutdown_timeout_seconds = (
            shutdown_timeout_seconds
            if shutdown_timeout_seconds is not None
            else _positive_float_env("DATAPAW_BLOCKING_SHUTDOWN_TIMEOUT_SECONDS", 10.0)
        )
        self.closed = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def run(
        self,
        pool: BlockingPool,
        operation: str,
        func: Callable[..., T],
        *args: Any,
        queue_timeout_seconds: float | None = None,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        if self.closed:
            raise BlockingIOOverloaded(
                pool,
                operation,
                "blocking I/O governor is shutting down",
            )
        return await self._pools[pool].run(
            operation,
            func,
            *args,
            queue_timeout_seconds=queue_timeout_seconds,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    def snapshot(self) -> dict[str, dict[str, int | float]]:
        return {name.value: pool.snapshot() for name, pool in self._pools.items()}

    def submit(
        self,
        pool: BlockingPool,
        operation: str,
        func: Callable[..., T],
        *args: Any,
        queue_timeout_seconds: float | None = None,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> asyncio.Task[T]:
        """Start governed background work and consume/log terminal failures."""

        task = asyncio.create_task(
            self.run(
                pool,
                operation,
                func,
                *args,
                queue_timeout_seconds=queue_timeout_seconds,
                timeout_seconds=timeout_seconds,
                **kwargs,
            ),
            name=f"datapaw-blocking-{pool.value}-{operation}",
        )
        self._background_tasks.add(task)

        def _complete(done: asyncio.Task[T]) -> None:
            self._background_tasks.discard(done)
            try:
                exception = done.exception()
            except asyncio.CancelledError:
                return
            if exception is not None:
                log.error(
                    "background blocking operation failed: pool=%s operation=%s",
                    pool.value,
                    operation,
                    exc_info=(
                        type(exception),
                        exception,
                        exception.__traceback__,
                    ),
                )

        task.add_done_callback(_complete)
        return task

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_timeout_seconds
        background = tuple(self._background_tasks)
        if background:
            _, unfinished = await asyncio.wait(
                background,
                timeout=self.shutdown_timeout_seconds,
            )
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        remaining = max(0.0, deadline - loop.time())
        await asyncio.gather(*[
            pool.aclose(remaining)
            for pool in self._pools.values()
        ])


__all__ = [
    "BlockingIOError",
    "BlockingIOGovernor",
    "BlockingIOOverloaded",
    "BlockingIOTimeout",
    "BlockingPool",
    "BlockingPoolConfig",
    "blocking_pool_configs_from_env",
]
