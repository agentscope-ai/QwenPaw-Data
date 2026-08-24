"""Event-loop lag monitoring regression test."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from context_manager.api.server import _event_loop_lag_monitor


async def test_event_loop_monitor_records_blocking_lag(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_EVENT_LOOP_PROBE_SECONDS", "0.01")
    monkeypatch.setenv("QWENPAW_DATA_EVENT_LOOP_LAG_WARN_SECONDS", "1")
    app = SimpleNamespace(state=SimpleNamespace())
    task = asyncio.create_task(_event_loop_lag_monitor(app))
    try:
        await asyncio.sleep(0.02)
        time.sleep(0.04)
        await asyncio.sleep(0.02)
        assert app.state.event_loop_max_lag_ms >= 20
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
