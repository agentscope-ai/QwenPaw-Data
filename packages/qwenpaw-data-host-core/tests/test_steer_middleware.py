# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import MagicMock

import pytest
from agentscope.event import HintBlockEvent
from agentscope.message import AssistantMsg, HintBlock, TextBlock

from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import ChatCommandRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.turn import compose_agent_input

_IDENTITY = Identity(
    user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID
)
_CHAT_ID = "chat_1"


def _agent(*, context: list[Any] | None = None) -> MagicMock:
    agent = MagicMock()
    agent.name = "QwenPawData"
    agent.state.reply_id = "reply-1"
    agent.state.context = list(context or [])
    return agent


def _downstream(*events: Any):
    async def next_handler(**_kwargs: Any) -> AsyncGenerator[Any, None]:
        for event in events:
            yield event

    return next_handler


def _run_context() -> RunContext:
    return RunContext(
        session_id="ses_1",
        chat_id=_CHAT_ID,
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=TEST_TENANT_ID,
            workspace_id=TEST_WORKSPACE_ID,
            user_id=TEST_USER_ID,
            agent_id="a1",
            session_id="ses_1",
        ),
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=_IDENTITY,
        session_factory=get_session_factory(),
    )


@pytest.fixture
async def enqueue_steer():
    """Seed Steer commands the way the HTTP router does (chat_commands rows)."""
    await reset_engine()
    await init_db()
    chat = SimpleNamespace(id=_CHAT_ID, identity=_IDENTITY)

    async def _enqueue(*items: tuple[str, list[dict[str, Any]]]) -> None:
        async with get_session_factory()() as db:
            for text, comments in items:
                await ChatCommandRepository(db).add(
                    ChatCommand.for_steer(chat, text, comments)
                )
            await db.commit()

    yield _enqueue
    await reset_engine()


@pytest.mark.asyncio
async def test_injects_before_downstream_reasoning_in_fifo_order(enqueue_steer) -> None:
    await enqueue_steer(("first", []), ("second", []))

    mw = SteerMiddleware(_run_context())
    agent = _agent()
    downstream = object()

    agen = mw.on_reasoning(
        agent=agent,
        input_kwargs={},
        next_handler=_downstream(downstream),
    )

    first = await agen.__anext__()
    assert isinstance(first, HintBlockEvent)
    assert first.source == "steer"
    assert first.hint == "first"

    second = await agen.__anext__()
    assert isinstance(second, HintBlockEvent)
    assert second.source == "steer"
    assert second.hint == "second"

    third = await agen.__anext__()
    assert first.metadata == {"artifact_comments": []}
    assert third is downstream

    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    hints = [
        block
        for msg in agent.state.context
        for block in msg.content
        if isinstance(block, HintBlock)
    ]
    assert [block.hint for block in hints] == ["first", "second"]
    assert all(block.source == "steer" for block in hints)


@pytest.mark.asyncio
async def test_hint_event_carries_artifact_comments(enqueue_steer) -> None:
    comments = [
        {
            "path": "russia_dau_mom.sql",
            "line_start": 2,
            "line_end": 10,
            "comment": "加一个参数",
        }
    ]
    await enqueue_steer(("nudge", comments))

    mw = SteerMiddleware(_run_context())
    agent = _agent()

    events = [
        event
        async for event in mw.on_reasoning(
            agent=agent,
            input_kwargs={},
            next_handler=_downstream(),
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], HintBlockEvent)
    assert events[0].hint == "nudge"
    assert events[0].metadata == {"artifact_comments": comments}
    hint_blocks = [
        block
        for msg in agent.state.context
        for block in msg.content
        if isinstance(block, HintBlock)
    ]
    assert [block.hint for block in hint_blocks] == [
        compose_agent_input("nudge", comments),
    ]


@pytest.mark.asyncio
async def test_appends_to_existing_assistant_message(enqueue_steer) -> None:
    await enqueue_steer(("nudge", []))

    existing = AssistantMsg(
        id="reply-1",
        name="QwenPawData",
        content=[TextBlock(text="thinking")],
    )
    agent = _agent(context=[existing])
    mw = SteerMiddleware(_run_context())

    events = [
        event
        async for event in mw.on_reasoning(
            agent=agent,
            input_kwargs={},
            next_handler=_downstream(),
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], HintBlockEvent)
    assert agent.state.context == [existing]
    assert isinstance(existing.content[-1], HintBlock)
    assert existing.content[-1].hint == "nudge"


@pytest.mark.asyncio
async def test_no_pending_passthrough(enqueue_steer) -> None:
    mw = SteerMiddleware(_run_context())
    agent = _agent()
    marker = object()

    events = [
        event
        async for event in mw.on_reasoning(
            agent=agent,
            input_kwargs={},
            next_handler=_downstream(marker),
        )
    ]

    assert events == [marker]
    assert agent.state.context == []


@pytest.mark.asyncio
async def test_cancelled_chat_never_injects_pending_steer(enqueue_steer) -> None:
    """Cancelling the chat discards queued steers before the next boundary.

    Cancellation used to release an in-process SteerQueue waiter; the queue is
    gone and ChatRuntime._finish now clears ``chat_commands`` (including
    waiting steers) when the chat ends.
    """
    await enqueue_steer(("cancel me", []))

    async with get_session_factory()() as db:
        await ChatCommandRepository(db).clear(_CHAT_ID)
        await db.commit()
        remaining = await ChatCommandRepository(db).take_steers(_CHAT_ID)
    assert remaining == []

    mw = SteerMiddleware(_run_context())
    agent = _agent()
    marker = object()

    events = [
        event
        async for event in mw.on_reasoning(
            agent=agent,
            input_kwargs={},
            next_handler=_downstream(marker),
        )
    ]

    assert events == [marker]
    assert agent.state.context == []
