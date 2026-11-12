# -*- coding: utf-8 -*-
"""Tests for the per-session serial queue that now lives on BaseChannel.

The queue state moved off the deleted ``SessionQueueManager`` onto each channel
instance (``BaseChannel._session_queues``). These tests exercise that behavior
directly through a minimal concrete ``BaseChannel`` subclass.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.channels.schema import NativePayload


class _TestChannel(BaseChannel):
    """Minimal concrete BaseChannel: records consumed payloads.

    ``_consume_one_request`` is overridden so the abstract streaming/send hooks
    are never reached; they remain no-ops.
    """

    channel = "test"

    def __init__(self, name: str = "test") -> None:
        super().__init__()
        self.channel = name
        self.consumed: list[Any] = []
        self._consume = AsyncMock(side_effect=self._record)

    async def _record(self, native: Any) -> None:
        self.consumed.append(native)

    async def _consume_one_request(self, native: NativePayload) -> None:  # type: ignore[override]
        await self._consume(native)

    # ---- abstract no-ops ----
    async def start(self) -> None:
        return None

    async def _stop(self) -> None:
        return None

    def resolve_session_id(self, native: NativePayload) -> str:
        return ""

    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        return None

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        return None

    async def send(self, native: NativePayload, text: str) -> None:
        return None

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        return None

    async def on_streaming_delta(
        self, native: NativePayload, handle: Any, delta: str
    ) -> None:
        return None

    async def on_streaming_end(
        self, native: NativePayload, handle: Any, full_text: str
    ) -> None:
        return None

    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        return None

    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        return None

    async def on_consume_start(self, native: NativePayload) -> None:
        return None

    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        return None

    async def send_image(self, native: NativePayload, path: str) -> None:
        return None

    async def send_file(self, native: NativePayload, path: str) -> None:
        return None

    async def send_clarification_card(
        self, native: NativePayload, questions: Any
    ) -> str:
        return ""

    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_stop_tears_down_this_channel_only() -> None:
    """_stop_session_queues removes this channel's queues; another instance is unaffected."""
    feishu = _TestChannel("feishu")
    wecom = _TestChannel("wecom")

    # Two sessions per channel.
    await feishu._enqueue_session("s1", "f1")
    await feishu._enqueue_session("s2", "f2")
    await wecom._enqueue_session("s1", "w1")
    await wecom._enqueue_session("s2", "w2")
    # Let the consumers process the items.
    await asyncio.sleep(0.01)

    assert len(feishu._session_queues) == 2
    assert feishu.consumed == ["f1", "f2"]
    assert wecom.consumed == ["w1", "w2"]

    n = await feishu._stop_session_queues()
    assert n == 2
    assert feishu._session_queues == {}
    # wecom's queues are intact and still drain new messages.
    assert len(wecom._session_queues) == 2
    await wecom._enqueue_session("s1", "w1b")
    await asyncio.sleep(0.01)
    assert wecom.consumed[-1] == "w1b"
    await wecom._stop_session_queues()


@pytest.mark.asyncio
async def test_stop_returns_zero_when_no_queues() -> None:
    ch = _TestChannel("feishu")
    assert await ch._stop_session_queues() == 0


@pytest.mark.asyncio
async def test_stop_lets_inflight_finish_gracefully() -> None:
    """An in-flight _consume_one_request is not cancelled; it completes before the consumer exits."""
    ch = _TestChannel("feishu")

    done = asyncio.Event()

    async def slow(_native: Any) -> None:
        await asyncio.sleep(0.05)
        done.set()

    ch._consume.side_effect = slow

    await ch._enqueue_session("s1", "x")  # starts the slow consume
    await asyncio.sleep(0.01)  # ensure it's mid-flight
    await ch._stop_session_queues()  # sentinel queued behind the running item
    assert await asyncio.wait_for(done.wait(), timeout=1.0)  # completed, not cancelled


@pytest.mark.asyncio
async def test_clear_targets_one_session_only() -> None:
    ch = _TestChannel("feishu")

    # Block consumption so items stay queued.
    block = asyncio.Event()

    async def blocked(_native: Any) -> None:
        await block.wait()

    ch._consume.side_effect = blocked

    await ch._enqueue_session("s1", "f1")
    await ch._enqueue_session("s1", "f2")
    await ch._enqueue_session("s2", "g1")
    await asyncio.sleep(0.01)

    n = await ch._clear_session_queue("s1")
    # f1 was already dequeued (blocked inside _consume_one_request); only f2 is still queued.
    assert n == 1
    # s2's consumer is untouched.
    assert "s2" in ch._session_queues

    block.set()
    await ch._stop_session_queues()
