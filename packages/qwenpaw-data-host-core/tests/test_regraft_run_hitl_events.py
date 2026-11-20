# -*- coding: utf-8 -*-
"""Re-graft B4: human-in-the-loop agent events and max-iterations stops.

agentscope may still yield RequireUserConfirmEvent under PermissionMode.BYPASS
(explicit ask rules in the permission context), while confirm results and
interrupts are agent inputs. None of them may crash the turn, and a
max-iterations stop must reach the client as a failed terminal response
instead of a silent completion.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.agent._config import ReActConfig
from agentscope.event import (
    ConfirmResult,
    ExceedMaxItersEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.runtime.envelope import Envelope

ENVELOPE_LOGGER = "qwenpaw_data.host.core.runtime.envelope"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class RecordingStream:
    """Stands in for OutputStream and records every write of the envelope."""

    def __init__(self) -> None:
        self.chat_id = "chat-hitl"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def record(*_args: Any, **kwargs: Any) -> None:
            self.calls.append((name, kwargs))

        return record


def _envelope() -> tuple[Envelope, RecordingStream]:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=None)  # type: ignore[arg-type]
    return envelope, stream


def _tool_call() -> ToolCallBlock:
    return ToolCallBlock(
        id="call-1", name="write_file", input=json.dumps({"path": "notes.txt"})
    )


async def test_hitl_agent_events_are_skipped_with_a_debug_log(caplog) -> None:
    envelope, stream = _envelope()
    call = _tool_call()
    events = [
        RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[call]),
        UserConfirmResultEvent(
            reply_id="reply-1",
            confirm_results=[ConfirmResult(confirmed=False, tool_call=call)],
        ),
        UserInterruptEvent(reply_id="reply-1"),
    ]

    with caplog.at_level(logging.DEBUG, logger=ENVELOPE_LOGGER):
        for event in events:
            await envelope.translate_event(event)

    assert stream.calls == []
    logged = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.name == ENVELOPE_LOGGER and record.levelno == logging.DEBUG
    )
    for name in ("REQUIRE_USER_CONFIRM", "USER_CONFIRM_RESULT", "USER_INTERRUPT"):
        assert name in logged


async def test_confirm_prompt_mid_message_keeps_the_turn_intact() -> None:
    envelope, stream = _envelope()

    await envelope.translate_event(
        TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.translate_event(
        TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="checking")
    )
    await envelope.translate_event(
        RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[_tool_call()])
    )
    await envelope.translate_event(
        TextBlockEndEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.complete()

    assert stream.calls[-1][0] == "response_completed"
    completed = [kwargs for name, kwargs in stream.calls if name == "message_complete"]
    assert len(completed) == 1
    assert completed[0]["content"][0]["text"] == "checking"


async def test_unknown_agent_event_still_fails_loudly() -> None:
    envelope, _stream = _envelope()

    with pytest.raises(ValueError, match="unsupported agent event"):
        await envelope.translate_event(SimpleNamespace(type="MYSTERY"))


async def test_max_iters_events_leave_the_terminal_to_the_runtime() -> None:
    envelope, stream = _envelope()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        exceeded = ExceedMaxItersEvent(reply_id="reply-1", name="agent")

    await envelope.translate_event(exceeded)
    await envelope.translate_event(
        ReplyEndEvent(
            session_id="ses-1",
            reply_id="reply-1",
            finished_reason=ReplyFinishedReason.EXCEED_MAX_ITERS,
        )
    )

    # No completion is synthesized here; ChatRuntime owns the failed terminal.
    assert stream.calls == []


class LoopingModel(ChatModelBase):
    """Fake model that keeps calling a tool and only answers when forced to."""

    def __init__(self) -> None:
        self.model = "max-iters-fake"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = SimpleNamespace(supported_input_media_types=[])
        self.calls = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list[Any],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, messages, tools, kwargs)
        self.calls += 1
        if getattr(tool_choice, "mode", None) == "none":
            content: list[Any] = [TextBlock(text="partial findings so far")]
        else:
            content = [
                ToolCallBlock(id=f"call-{self.calls}", name="lookup", input="{}")
            ]
        return ChatResponse(
            content=content,
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


class _LeaseManager:
    """Workspace manager stand-in: a lease without any sandbox behind it."""

    @asynccontextmanager
    async def lease(self, _paths: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(workspace_id="b4-test", is_alive=True)


def _runtime_config(max_iters: int) -> Any:
    from qwenpaw_data.host.core.domain.preference import (
        ActiveModel,
        UserRuntimeConfig,
    )

    model = ActiveModel(
        provider_id="openai",
        model_id="max-iters-fake",
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        chat_model="OpenAIChatModel",
        name="max-iters-fake",
    )
    return UserRuntimeConfig(
        default=model,
        light=None,
        react_max_iters=max_iters,
        llm_retry_enabled=False,
        llm_max_retries=0,
        context_trigger_ratio=0.8,
        context_reserve_ratio=0.2,
        tool_result_limit=10000,
        tool_timeout_enabled=False,
        tool_timeout_seconds=60.0,
        tool_confirmation_level="auto",
    )


@pytest.fixture
async def chat_db(tmp_path, monkeypatch):
    from qwenpaw_data.host.core.db.engine import (
        get_session_factory,
        init_db,
        reset_engine,
    )
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.domain.session import Session
    from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'host.db'}"
    )
    clear_settings()
    await reset_engine()
    await init_db()
    factory = get_session_factory()
    identity = Identity(tenant_id="t", workspace_id="w", user_id="u")
    async with factory() as db:
        session = Session.create(identity=identity, agent_id="agent", title="b4")
        chat = session.open_chat(
            text="keep digging", datasource_id=None, has_active_chat=False
        )
        await SessionRepository(db, identity).add(session)
        await ChatRepository(db, identity).add(chat)
        await db.commit()
    try:
        yield SimpleNamespace(factory=factory, identity=identity, chat=chat)
    finally:
        await reset_engine()
        clear_settings()


async def test_fake_model_hitting_max_iterations_fails_the_chat(
    chat_db, monkeypatch
) -> None:
    from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
    from qwenpaw_data.host.core.repositories import ChatRepository
    from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
    from qwenpaw_data.host.core.runtime import chat_runtime as chat_runtime_module
    from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime

    model = LoopingModel()
    agent = QwenPawDataAgent(
        name="qwenpaw-data",
        system_prompt="",
        model=model,
        toolkit=Toolkit(),
        react_config=ReActConfig(
            max_iters=1, interruption_raise_cancelled_error=True
        ),
    )
    config = _runtime_config(max_iters=1)
    monkeypatch.setattr(
        chat_runtime_module, "get_workspace_manager", lambda: _LeaseManager()
    )
    monkeypatch.setattr(
        chat_runtime_module.PreferencesRepository,
        "load",
        AsyncMock(return_value=SimpleNamespace(resolve=lambda: config)),
    )
    monkeypatch.setattr(
        chat_runtime_module.McpRepository, "sync", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        chat_runtime_module, "fetch_datasource_type", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        chat_runtime_module,
        "get_agent_builder",
        lambda _agent_id: SimpleNamespace(build=AsyncMock(return_value=agent)),
    )
    runtime = ChatRuntime(chat_db.factory, chat_db.chat.id)
    monkeypatch.setattr(runtime, "_log_chat_start", lambda *_args: None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        await runtime.run(identity=chat_db.identity)

    async with chat_db.factory() as db:
        saved = await ChatRepository(db, chat_db.identity).get(chat_db.chat.id)
        events = await ChatEventRepository(db, chat_db.identity).read_after(
            chat_db.chat.id, -1
        )

    assert model.calls >= 2  # one tool round plus the forced finalization
    assert saved.status == "failed"
    assert saved.error is not None
    assert saved.error["code"] == "REACT_MAX_ITERS"
    terminal = [
        event
        for event in events
        if event.object == "response" and event.status in TERMINAL_STATUSES
    ]
    assert [event.status for event in terminal] == ["failed"]
    assert events[-1] is terminal[0]
    assert terminal[0].error.model_dump() == saved.error
