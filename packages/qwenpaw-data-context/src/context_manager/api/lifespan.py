"""Connect module-owned services to FastAPI startup and shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _positive_float_env(name: str, default: float, minimum: float) -> float:
    """Read a positive floating-point setting without breaking startup."""
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


async def _event_loop_lag_monitor(application: FastAPI) -> None:
    """Continuously record event-loop scheduling lag on application state."""
    interval = _positive_float_env(
        "QWENPAW_DATA_EVENT_LOOP_PROBE_SECONDS",
        1.0,
        0.01,
    )
    warning = _positive_float_env(
        "QWENPAW_DATA_EVENT_LOOP_LAG_WARN_SECONDS",
        0.2,
        0.001,
    )
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    application.state.event_loop_lag_ms = 0
    application.state.event_loop_max_lag_ms = 0
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        lag_ms = max(0, round((now - expected) * 1000))
        application.state.event_loop_lag_ms = lag_ms
        application.state.event_loop_max_lag_ms = max(
            application.state.event_loop_max_lag_ms,
            lag_ms,
        )
        if lag_ms >= warning * 1000:
            logger.warning("event loop lag detected: %dms", lag_ms)
        expected = now + interval


@asynccontextmanager
async def api_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Connect Runtime and protocol sessions to FastAPI's lifetime."""
    runtime = getattr(application.state, "context_runtime", None)
    lag_monitor: asyncio.Task[None] | None = None
    try:
        if runtime is not None:
            await runtime.start()
        lag_monitor = asyncio.create_task(
            _event_loop_lag_monitor(application),
            name="qwenpaw-data-event-loop-lag-monitor",
        )
        async with AsyncExitStack() as stack:
            mcp = getattr(application.state, "context_mcp", None)
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
            yield
    finally:
        if lag_monitor is not None:
            lag_monitor.cancel()
            try:
                await lag_monitor
            except asyncio.CancelledError:
                pass
        if runtime is not None:
            await runtime.aclose()


__all__ = ["_event_loop_lag_monitor", "api_lifespan"]
