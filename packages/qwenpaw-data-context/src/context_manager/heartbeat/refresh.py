"""Keep one owned row alive until the caller stops the loop."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from ..settings import get_settings
from ..utils.heartbeat import heartbeat_is_fresh

log = logging.getLogger(__name__)


def stale_after_seconds() -> float:
    """Return the deployment age after which another worker may take a row."""
    return get_settings().heartbeat.stale_after_seconds


def stale_cutoff(now: float | None = None) -> float:
    """Return the oldest heartbeat that is still fresh at ``now``."""
    current = time.time() if now is None else now
    return current - stale_after_seconds()


def is_fresh(heartbeat_at: float | None, *, now: float | None = None) -> bool:
    """Use the deployment stale window. ``now`` is for tests that freeze time."""
    current = time.time() if now is None else now
    return heartbeat_is_fresh(
        heartbeat_at,
        now=current,
        fresh_seconds=stale_after_seconds(),
    )


class Heartbeat:
    """Refresh one row on a fixed interval.

    A failed refresh is logged and the loop continues. Losing the row is the
    caller's check inside ``touch``, not an exception from this loop.
    """

    def __init__(
        self,
        touch: Callable[[], Awaitable[None] | None],
        *,
        interval: float | None = None,
    ) -> None:
        self._touch = touch
        self._interval = (
            get_settings().heartbeat.interval_seconds if interval is None else interval
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("heartbeat already started")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await self._task
        except Exception:
            log.debug("heartbeat loop ended with an error", exc_info=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self._touch()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.debug("heartbeat refresh failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
