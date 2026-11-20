# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.event import (
    ExceedMaxItersEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    TextBlockDeltaEvent,
)

from qwenpaw_data.host.core.domain.exception import IterationLimitExceeded
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.executor import AgentExecutor
from qwenpaw_data.host.core.runtime.turn import TurnInput


class _Envelope:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def translate_event(self, event: Any) -> None:
        self.events.append(event)


class _Agent:
    def __init__(self, events: list[Any]) -> None:
        self.name = "qwenpaw-data"
        self._events = events

    async def reply_stream(self, _inputs: Any):  # noqa: ANN201
        for event in self._events:
            yield event


def _executor() -> AgentExecutor:
    return AgentExecutor(
        SimpleNamespace(
            session_factory=None,
            chat_id="chat-1",
            session_id="ses-1",
            identity=Identity(user_id="u", tenant_id="t", workspace_id="w"),
            request_context={},
            user_runtime_config=SimpleNamespace(tool_confirmation_level="auto"),
        )
    )  # type: ignore[arg-type]


def _turn() -> TurnInput:
    return TurnInput(chat_id="chat-1", user_input="hello")


@pytest.mark.filterwarnings(
    "ignore:ExceedMaxItersEvent is deprecated:DeprecationWarning"
)
async def test_reply_end_exceed_max_iters_raises_after_the_event_is_delivered() -> None:
    delivered: list[Any] = []

    async def after_event(event: Any) -> None:
        delivered.append(event)

    reply_end = ReplyEndEvent(
        session_id="session-1",
        reply_id="reply-1",
        finished_reason=ReplyFinishedReason.EXCEED_MAX_ITERS,
    )
    events = [
        TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="partial"),
        ExceedMaxItersEvent(reply_id="reply-1", name="qwenpaw-data"),
        reply_end,
    ]
    envelope = _Envelope()
    executor = _executor()
    task = executor.start(
        _Agent(events),  # type: ignore[arg-type]
        _turn(),
        envelope,  # type: ignore[arg-type]
        after_event_callback=after_event,
    )

    with pytest.raises(IterationLimitExceeded, match="qwenpaw-data") as raised:
        await task

    assert raised.value.agent_name == "qwenpaw-data"
    assert envelope.events == events
    assert delivered == events


@pytest.mark.filterwarnings(
    "ignore:ExceedMaxItersEvent is deprecated:DeprecationWarning"
)
async def test_deprecated_exceed_max_iters_event_alone_completes() -> None:
    envelope = _Envelope()
    await _executor().run(
        _Agent([ExceedMaxItersEvent(reply_id="reply-1", name="qwenpaw-data")]),  # type: ignore[arg-type]
        _turn(),
        envelope,  # type: ignore[arg-type]
    )
    assert len(envelope.events) == 1


async def test_completed_reply_end_does_not_raise() -> None:
    envelope = _Envelope()
    await _executor().run(
        _Agent(
            [
                ReplyEndEvent(
                    session_id="session-1",
                    reply_id="reply-1",
                    finished_reason=ReplyFinishedReason.COMPLETED,
                )
            ]
        ),  # type: ignore[arg-type]
        _turn(),
        envelope,  # type: ignore[arg-type]
    )
    assert len(envelope.events) == 1


def test_iteration_limit_requires_agent_name() -> None:
    with pytest.raises(ValueError, match="agent_name is required"):
        IterationLimitExceeded("  ")
