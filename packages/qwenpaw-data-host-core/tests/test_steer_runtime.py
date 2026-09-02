# -*- coding: utf-8 -*-
"""Steer queue and middleware behavior for the ported runtime."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agentscope.event import HintBlockEvent

from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.domain.steer import (
    SteerChatEndedError,
    SteerQueue,
    SteerStateError,
)
from qwenpaw_data.host.core.runtime.executor import AgentExecutor


async def test_steer_queue_fifo_inject_and_ack() -> None:
    queue = SteerQueue()

    waiter = asyncio.create_task(queue.wait_until_injected("go deeper"))
    await asyncio.sleep(0)

    pending = await queue.take_pending()
    assert [item.text for item in pending] == ["go deeper"]
    assert pending[0].status == "injecting"

    await queue.mark_injected(pending[0])
    await waiter  # resolves without error


async def test_steer_queue_cancel_all_releases_waiters_and_closes() -> None:
    queue = SteerQueue()
    waiter = asyncio.create_task(queue.wait_until_injected("too late"))
    await asyncio.sleep(0)

    await queue.cancel_all()
    with pytest.raises(SteerChatEndedError):
        await waiter

    assert queue.closed
    with pytest.raises(SteerChatEndedError):
        await queue.wait_until_injected("after close")
    assert await queue.take_pending() == []


async def test_steer_queue_rejects_illegal_transitions() -> None:
    queue = SteerQueue()
    task = asyncio.create_task(queue.wait_until_injected("x"))
    await asyncio.sleep(0)
    (item,) = await queue.take_pending()
    await queue.mark_injected(item)
    with pytest.raises(SteerStateError):
        await queue.mark_injected(item)
    await task

    with pytest.raises(ValueError):
        await queue.wait_until_injected("   ")


class FakeState:
    def __init__(self) -> None:
        self.reply_id = "reply-1"
        self.context: list[Any] = []


class FakeAgent:
    def __init__(self, middleware: SteerMiddleware) -> None:
        self.name = "qwenpaw-data"
        self.state = FakeState()
        self._reasoning_middlewares = [middleware]


async def test_middleware_injects_pending_steers_as_hints() -> None:
    middleware = SteerMiddleware()
    agent = FakeAgent(middleware)

    steer_task = asyncio.create_task(middleware.steer("focus on Q3"))
    await asyncio.sleep(0)

    async def next_handler(**_kwargs: Any):
        yield "sentinel-event"

    events = [
        event
        async for event in middleware.on_reasoning(agent, {}, next_handler)
    ]
    await steer_task

    hint_events = [e for e in events if isinstance(e, HintBlockEvent)]
    assert len(hint_events) == 1
    assert hint_events[0].hint == "focus on Q3"
    assert hint_events[0].source == "steer"
    assert events[-1] == "sentinel-event"
    # The hint landed in the model context as an assistant-message block.
    assert len(agent.state.context) == 1
    assert agent.state.context[0].content[0].hint == "focus on Q3"


async def test_middleware_reset_reopens_after_cancel() -> None:
    middleware = SteerMiddleware()
    await middleware.cancel()
    assert middleware.queue.closed

    middleware.reset()
    assert not middleware.queue.closed

    task = asyncio.create_task(middleware.steer("next turn steer"))
    await asyncio.sleep(0)
    (item,) = await middleware.queue.take_pending()
    await middleware.queue.mark_injected(item)
    await task


async def test_middleware_find_and_require() -> None:
    middleware = SteerMiddleware()
    agent = FakeAgent(middleware)
    assert SteerMiddleware.find(agent) is middleware
    assert SteerMiddleware.require(agent) is middleware

    bare = FakeAgent(middleware)
    bare._reasoning_middlewares = []
    assert SteerMiddleware.find(bare) is None
    with pytest.raises(RuntimeError, match="CONFLICT"):
        SteerMiddleware.require(bare)


async def test_executor_steer_requires_ready_agent() -> None:
    executor = AgentExecutor()
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await executor.steer("too early")
    # cancel_steer before any agent is a no-op
    await executor.cancel_steer()
