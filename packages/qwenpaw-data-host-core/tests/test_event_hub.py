from __future__ import annotations

import asyncio

import pytest

from pydantic import ValidationError

from qwenpaw_data.host.core.settings import StreamSettings
from qwenpaw_data.host.core.stream.hub import EventHub


def test_live_subscription_emits_heartbeat_while_idle() -> None:
    async def scenario() -> None:
        hub = EventHub()
        subscription = hub.subscribe_live("chat-1", heartbeat_interval=0.01)

        assert await anext(subscription) is None

        await hub.close("chat-1")
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)

    asyncio.run(scenario())


def test_stream_settings_default_sse_heartbeat() -> None:
    assert StreamSettings().sse_heartbeat_seconds == 15
    assert StreamSettings().heartbeat_ttl_seconds == 30
    assert StreamSettings().heartbeat_interval_seconds == 10


def test_stream_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS", "30")
    assert StreamSettings().sse_heartbeat_seconds == 30


def test_stream_settings_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        StreamSettings(sse_heartbeat_seconds=0)
