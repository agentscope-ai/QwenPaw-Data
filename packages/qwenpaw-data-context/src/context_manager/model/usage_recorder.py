"""Flush bounded usage batches with best-effort retries and shutdown."""

from __future__ import annotations

import logging
from collections import deque
from threading import Condition, Thread
from time import monotonic

from .usage import ModelUsageEvent
from .repository import ModelUsageRepository

log = logging.getLogger(__name__)


class ModelUsageRecorder:
    """Own one daemon writer; only explicit start enables event admission."""

    def __init__(
        self,
        repository: ModelUsageRepository,
        *,
        queue_size: int = 10000,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        max_retries: int = 3,
        retry_base: float = 0.25,
    ) -> None:
        if queue_size < 1 or batch_size < 1:
            raise ValueError("usage queue and batch sizes must be positive")
        if flush_interval < 0 or max_retries < 0 or retry_base < 0:
            raise ValueError("usage intervals and retries must be non-negative")
        self._repository = repository
        self._queue_size = queue_size
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_retries = max_retries
        self._retry_base = retry_base
        self._queue: deque[ModelUsageEvent] = deque()
        self._condition = Condition()
        self._thread: Thread | None = None
        self._deadline: float | None = None
        self._dropped = 0

    @property
    def usage_dropped_total(self) -> int:
        with self._condition:
            return self._dropped

    def start(self) -> None:
        with self._condition:
            if self._thread is not None or self._deadline is not None:
                return
            self._thread = Thread(target=self._run, name="cm-model-usage", daemon=True)
            self._thread.start()

    def enqueue(self, event: ModelUsageEvent) -> bool:
        with self._condition:
            if self._thread is None or self._deadline is not None:
                self._drop(1, "not accepting")
                return False
            if len(self._queue) >= self._queue_size:
                self._drop(1, "queue full")
                return False
            self._queue.append(event)
            self._condition.notify()
            return True

    def stop(self, timeout: float = 3.0) -> bool:
        """Stop admission and join within the first shutdown deadline."""
        with self._condition:
            if self._deadline is None:
                self._deadline = monotonic() + max(0.0, timeout)
            self._condition.notify_all()
            thread = self._thread
            remaining = min(max(0.0, timeout), max(0.0, self._deadline - monotonic()))
        if thread is not None:
            thread.join(remaining)
        with self._condition:
            if self._expired():
                self._discard_queue()
        stopped = thread is None or not thread.is_alive()
        if not stopped:
            log.warning(
                "Model usage writer is still running after the shutdown deadline"
            )
        return stopped

    def _expired(self) -> bool:
        return self._deadline is not None and monotonic() >= self._deadline

    def _drop(self, count: int, reason: str) -> None:
        if count:
            self._dropped += count
            log.warning(
                "Dropped %s model usages (%s); total=%s", count, reason, self._dropped
            )

    def _discard_queue(self) -> None:
        self._drop(len(self._queue), "shutdown deadline")
        self._queue.clear()

    def _run(self) -> None:
        with self._condition:
            if self._expired():
                self._discard_queue()
                return
        try:
            self._repository.probe()
        except Exception as exc:
            log.warning(
                "Model usage probe failed (%s); continuing writes", type(exc).__name__
            )
        while events := self._next_batch():
            self._write(events)

    def _next_batch(self) -> tuple[ModelUsageEvent, ...]:
        events: list[ModelUsageEvent] = []
        flush_at: float | None = None
        with self._condition:
            while True:
                if self._expired():
                    self._drop(len(events), "shutdown deadline")
                    self._discard_queue()
                    return ()
                while self._queue and len(events) < self._batch_size:
                    events.append(self._queue.popleft())
                    if flush_at is None:
                        flush_at = monotonic() + self._flush_interval
                stopping = self._deadline is not None
                if len(events) == self._batch_size or (
                    events and (stopping or monotonic() >= flush_at)
                ):
                    return tuple(events)
                if stopping:
                    return ()
                self._condition.wait(
                    None if flush_at is None else max(0.0, flush_at - monotonic())
                )

    def _write(self, events: tuple[ModelUsageEvent, ...]) -> None:
        for attempt in range(self._max_retries + 1):
            with self._condition:
                if self._expired():
                    self._drop(len(events), "shutdown deadline")
                    return
            # A write already admitted may block beyond stop's deadline.
            try:
                self._repository.write(events)
                return
            except Exception as exc:
                log.warning("Model usage write failed (%s)", type(exc).__name__)
            with self._condition:
                if attempt == self._max_retries:
                    self._drop(len(events), "retries exhausted")
                    return
                retry_at = monotonic() + self._retry_base * (2**attempt)
                while True:
                    if self._expired():
                        self._drop(len(events), "shutdown deadline")
                        return
                    remaining = retry_at - monotonic()
                    if remaining <= 0:
                        break
                    if self._deadline is not None:
                        remaining = min(remaining, self._deadline - monotonic())
                    self._condition.wait(max(0.0, remaining))
