# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.event import (
    ExternalExecutionResultEvent,
    ModelCallStartEvent,
    RequireExternalExecutionEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState

from qwenpaw_data.host.core.agent.middleware.trace_forward import TraceForwardMiddleware
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.domain.clarification import ABSENT_USER_CLARIFICATION_TEXT
from qwenpaw_data.host.core.repositories import ChatCommandRepository, ChatRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.executor import AgentExecutor
from qwenpaw_data.host.core.runtime.turn import TurnInput

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)

_ANSWERED: dict[str, Any] = {
    "status": "answered",
    "answers": [
        {
            "question": "Which?",
            "selected_options": ["A"],
            "custom_text": None,
        }
    ],
}
_TIMEOUT: dict[str, Any] = {"status": "timeout", "reason": "timed out"}


def _run_context(chat_id: str) -> RunContext:
    return RunContext(
        session_id="ses-1",
        chat_id=chat_id,
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=TEST_TENANT_ID,
            workspace_id=TEST_WORKSPACE_ID,
            user_id=TEST_USER_ID,
            agent_id="default",
            session_id="ses-1",
        ),
        user_runtime_config=SimpleNamespace(
            default=SimpleNamespace(),
            tool_confirmation_level="auto",
        ),
        identity=TEST_IDENTITY,
        session_factory=get_session_factory(),
    )


def _chat(chat_id: str) -> Chat:
    chat = Chat.start(
        session_id="ses-1",
        identity=TEST_IDENTITY,
        sequence=1,
        text="hello",
        datasource_id=None,
    )
    chat.id = chat_id
    return chat


_TOOL_INPUT = {
    "title": "Confirm",
    "questions": [{"question": "Color?"}, {"question": "Unit?"}],
}


async def _persist_chat(chat_id: str, *, enable_clarification: bool) -> None:
    chat = _chat(chat_id)
    chat.enable_clarification = enable_clarification
    async with get_session_factory()() as db:
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()


async def _write_answer(
    chat_id: str,
    clarification_id: str,
    result: dict[str, Any],
) -> None:
    """Frontend answers a clarification by writing a command row."""
    async with get_session_factory()() as db:
        await ChatCommandRepository(db).add(
            ChatCommand.clarification(
                _chat(chat_id), clarification_id=clarification_id, result=result
            )
        )
        await db.commit()


class FakeEnvelope:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def translate_event(self, event: Any) -> None:
        self.events.append(event)


class FakeTraceForward(TraceForwardMiddleware):
    def __init__(self) -> None:
        self.ended = 0

    async def end_stream(self) -> None:
        self.ended += 1


class FakeAgent:
    def __init__(self, tool_input: Any = "{}") -> None:
        self.inputs: list[Any] = []
        self.external_result_saved = False
        self.model_called = False
        self.waiting = asyncio.Event()
        self._tool_input = tool_input
        # TraceForwardMiddleware.find walks this agentscope list.
        self._reasoning_middlewares: list[Any] = []
        self._reply_middlewares: list[Any] = []

    async def reply_stream(self, inputs: Any):  # noqa: ANN201
        self.inputs.append(inputs)
        if not isinstance(inputs, ExternalExecutionResultEvent):
            yield ToolCallStartEvent(
                reply_id="reply-1",
                tool_call_id="call-1",
                tool_call_name="ask_user_question",
            )
            yield ToolCallEndEvent(
                reply_id="reply-1",
                tool_call_id="call-1",
            )
            yield ToolResultStartEvent(
                reply_id="reply-1",
                tool_call_id="call-1",
                tool_call_name="ask_user_question",
            )
            self.waiting.set()
            yield RequireExternalExecutionEvent(
                reply_id="reply-1",
                tool_calls=[
                    ToolCallBlock(
                        id="call-1",
                        name="ask_user_question",
                        input=self._tool_input,
                    )
                ],
            )
            return

        assert isinstance(inputs, ExternalExecutionResultEvent)
        result = inputs.execution_results[0]
        yield ToolResultTextDeltaEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            delta=result.output[0].text,
        )
        yield ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            state=ToolResultState.SUCCESS,
        )
        self.external_result_saved = True
        yield ModelCallStartEvent(
            reply_id="reply-1",
            model_name="fake-model",
        )
        self.model_called = True


async def _wait_until_waiting(agent: FakeAgent) -> None:
    """Wait for the executor to park on its clarification command poll."""
    await asyncio.wait_for(agent.waiting.wait(), timeout=1)
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_answer_resumes_the_same_agent_and_keeps_metadata() -> None:
    await reset_engine()
    await init_db()
    try:
        chat_id = "chat-1"
        executor = AgentExecutor(_run_context(chat_id))
        agent = FakeAgent()
        envelope = FakeEnvelope()

        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id=chat_id, user_input="hello"),
            envelope,  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)

        await _write_answer(chat_id, "call-1", _ANSWERED)
        await asyncio.wait_for(task, timeout=1)

        assert len(agent.inputs) == 2
        external = agent.inputs[1]
        assert isinstance(external, ExternalExecutionResultEvent)
        assert json.loads(external.execution_results[0].output[0].text) == _ANSWERED
        assert agent.external_result_saved is True
        assert agent.model_called is True

        tool_call_start = envelope.events[0]
        assert tool_call_start.metadata["expires_at"].endswith("Z")
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_timeout_cancels_without_writing_tool_result() -> None:
    await reset_engine()
    await init_db()
    try:
        chat_id = "chat-1"
        executor = AgentExecutor(_run_context(chat_id))
        agent = FakeAgent()
        envelope = FakeEnvelope()

        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id=chat_id, user_input="hello"),
            envelope,  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)
        await _write_answer(chat_id, "call-1", _TIMEOUT)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert agent.external_result_saved is False
        assert agent.model_called is False
        assert not any(
            isinstance(event, ToolResultEndEvent) for event in envelope.events
        )
        assert not any(
            isinstance(event, ModelCallStartEvent) for event in envelope.events
        )
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_answer_requires_matching_and_unconsumed_call_id() -> None:
    await reset_engine()
    await init_db()
    try:
        chat_id = "chat-1"

        # Nothing is awaiting: no command to consume.
        async with get_session_factory()() as db:
            assert (
                await ChatCommandRepository(db).take_clarification(chat_id, "call-1")
                is None
            )

        executor = AgentExecutor(_run_context(chat_id))
        agent = FakeAgent()
        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id=chat_id, user_input="hello"),
            FakeEnvelope(),  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)

        # A command for a different clarification id is never delivered.
        await _write_answer(chat_id, "wrong", _TIMEOUT)
        await asyncio.sleep(0.2)
        assert not task.done()

        # The matching id resumes the one pending clarification.
        await _write_answer(chat_id, "call-1", _ANSWERED)
        await asyncio.wait_for(task, timeout=1)
        assert len(agent.inputs) == 2

        # A duplicate answer for the resolved id is not delivered again.
        await _write_answer(chat_id, "call-1", _ANSWERED)
        await asyncio.sleep(0.2)
        assert len(agent.inputs) == 2
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.executor.close_interrupted_tool_calls",
        lambda _agent: None,
    )
    await reset_engine()
    await init_db()
    try:
        executor = AgentExecutor(_run_context("chat-1"))
        agent = FakeAgent()
        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id="chat-1", user_input="hello"),
            FakeEnvelope(),  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)

        await executor.stop()

        assert task.cancelled()
        assert agent.external_result_saved is False
        assert len(agent.inputs) == 1
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stop_ends_the_trace_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.executor.close_interrupted_tool_calls",
        lambda _agent: None,
    )
    await reset_engine()
    await init_db()
    try:
        executor = AgentExecutor(_run_context("chat-1"))
        agent = FakeAgent()
        forward = FakeTraceForward()
        agent._reply_middlewares = [forward]
        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id="chat-1", user_input="hello"),
            FakeEnvelope(),  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)

        await executor.stop()

        assert task.cancelled()
        assert forward.ended == 1
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_disabled_clarification_answers_without_frontend() -> None:
    await reset_engine()
    await init_db()
    try:
        chat_id = "chat-1"
        await _persist_chat(chat_id, enable_clarification=False)
        executor = AgentExecutor(_run_context(chat_id))
        agent = FakeAgent(tool_input=json.dumps(_TOOL_INPUT, ensure_ascii=False))
        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id=chat_id, user_input="hello"),
            FakeEnvelope(),  # type: ignore[arg-type]
        )
        await asyncio.wait_for(task, timeout=1)

        assert len(agent.inputs) == 2
        result = json.loads(agent.inputs[1].execution_results[0].output[0].text)
        assert result["status"] == "answered"
        assert [item["question"] for item in result["answers"]] == ["Color?", "Unit?"]
        assert all(
            item["custom_text"] == ABSENT_USER_CLARIFICATION_TEXT
            for item in result["answers"]
        )
        assert agent.external_result_saved is True
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_clarification_flag_flip_unblocks_waiting_executor() -> None:
    await reset_engine()
    await init_db()
    try:
        chat_id = "chat-1"
        await _persist_chat(chat_id, enable_clarification=True)
        executor = AgentExecutor(_run_context(chat_id))
        agent = FakeAgent(tool_input=json.dumps(_TOOL_INPUT, ensure_ascii=False))
        task = executor.start(
            agent,  # type: ignore[arg-type]
            TurnInput(chat_id=chat_id, user_input="hello"),
            FakeEnvelope(),  # type: ignore[arg-type]
        )
        await _wait_until_waiting(agent)
        assert not task.done()

        async with get_session_factory()() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            chat.set_enable_clarification(False)
            await ChatRepository(db, TEST_IDENTITY).save(chat)
            await db.commit()

        await asyncio.wait_for(task, timeout=2)
        result = json.loads(agent.inputs[1].execution_results[0].output[0].text)
        assert result["status"] == "answered"
    finally:
        await reset_engine()
