# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from agentscope.agent._config import ReActConfig
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.executor import AgentExecutor
from qwenpaw_data.host.core.runtime.turn import TurnInput
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)
_CHAT_ID = "chat_1"


class _StubModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "test"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()

    async def _call_api(self, *args: Any, **kwargs: Any) -> ChatResponse:
        _ = (args, kwargs)
        return ChatResponse(
            content=[TextBlock(text="ok")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def _run_context(session_factory: Any = None) -> RunContext:
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
        user_runtime_config=SimpleNamespace(tool_confirmation_level="auto"),  # type: ignore[arg-type]
        identity=TEST_IDENTITY,
        session_factory=session_factory or get_session_factory(),
    )


def _downstream(*events: Any):
    async def next_handler(**_kwargs: Any) -> AsyncGenerator[Any, None]:
        for event in events:
            yield event

    return next_handler


def _stub_agent(
    *,
    run_context: RunContext | None = None,
) -> tuple[QwenPawDataAgent, SteerMiddleware]:
    middleware = SteerMiddleware(run_context or _run_context())
    agent = QwenPawDataAgent(
        name="qwenpaw-data",
        system_prompt="",
        model=_StubModel(),
        toolkit=Toolkit(tools=[]),
        middlewares=[middleware],
        react_config=ReActConfig(max_iters=10),
    )
    return agent, middleware


class BlockingThenTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "blocking-steer-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()
        self.first_call_started = asyncio.Event()
        self.continue_first_call = asyncio.Event()

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, messages, tools, tool_choice, kwargs)
        if not self.first_call_started.is_set():
            self.first_call_started.set()
            await self.continue_first_call.wait()
        return ChatResponse(
            content=[TextBlock(text="done")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


class FakeEnvelope:
    """Only the seam AgentExecutor.run uses to forward stream events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def translate_event(self, event: Any) -> None:
        self.events.append(event)


class _NoopTransformer:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        return None

    async def append(self, entry: Any, *, last: bool = False) -> None:
        return None

    async def join(self) -> None:
        return None


async def _steer_count(chat_id: str) -> int:
    async with get_session_factory()() as db:
        return len(await ChatCommandRepository(db).take_steers(chat_id))


async def _enqueue_steer(chat_id: str, text: str) -> None:
    async with get_session_factory()() as db:
        chat = SimpleNamespace(id=chat_id, identity=TEST_IDENTITY)
        await ChatCommandRepository(db).add(ChatCommand.steer(chat, text, []))
        await db.commit()


async def _prepare_blocked_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
):
    await reset_engine()
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    monkeypatch.setattr(
        "qwenpaw_data.host.core.agent.middleware.trace_forward.BizTraceTransformer",
        _NoopTransformer,
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        AsyncMock(return_value=[]),
    )
    model = BlockingThenTextModel()
    monkeypatch.setattr(builder_module, "build_model", lambda *_a, **_k: model)
    await init_db()
    session_factory = get_session_factory()

    async with session_factory() as db:
        session = Session.create(identity=TEST_IDENTITY, title="steer")
        await SessionRepository(db, TEST_IDENTITY).add(session)
        chat = session.open_chat(
            text="start",
            datasource_id="ds",
            has_active_chat=False,
        )
        await SessionRepository(db, TEST_IDENTITY).save(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()

    await seed_test_preferences(session_factory)
    runtime = ChatRuntime(session_factory, chat.id)
    run_task = asyncio.create_task(runtime.run(identity=TEST_IDENTITY))
    await asyncio.wait_for(model.first_call_started.wait(), timeout=5)
    return model, runtime, run_task, session_factory, chat


async def _cleanup_runtime(
    model: BlockingThenTextModel,
    run_task: asyncio.Task[None],
) -> None:
    model.continue_first_call.set()
    if not run_task.done():
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
    reset_hub()
    await reset_engine()


@pytest.mark.asyncio
async def test_steer_without_reasoning_boundary_dropped_when_chat_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    model, _runtime, run_task, session_factory, chat = await _prepare_blocked_runtime(
        tmp_path,
        monkeypatch,
        seed_test_preferences,
    )
    try:
        # The user steers while the agent is parked inside its first reasoning
        # call, so the command can never reach the on_reasoning boundary.
        await _enqueue_steer(chat.id, "late nudge")
        assert await _steer_count(chat.id) == 1

        model.continue_first_call.set()
        await asyncio.wait_for(run_task, timeout=5)

        async with session_factory() as db:
            saved = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
        assert saved.status == "completed"
        # ChatRuntime._finish clears chat_commands, so the undelivered steer is
        # dropped instead of lingering for a chat that already ended.
        assert await _steer_count(chat.id) == 0
    finally:
        await _cleanup_runtime(model, run_task)


@pytest.mark.asyncio
async def test_cancel_releases_pending_steer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    model, runtime, run_task, _session_factory, chat = await _prepare_blocked_runtime(
        tmp_path,
        monkeypatch,
        seed_test_preferences,
    )
    try:
        await _enqueue_steer(chat.id, "will be cancelled")
        assert await _steer_count(chat.id) == 1

        await runtime.cancel()
        await asyncio.wait_for(run_task, timeout=5)
        assert await _steer_count(chat.id) == 0
    finally:
        await _cleanup_runtime(model, run_task)


@pytest.mark.asyncio
async def test_runtime_agent_not_ready_before_run() -> None:
    runtime = ChatRuntime(None, "chat-1")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="agent is not ready"):
        runtime.agent


@pytest.mark.asyncio
async def test_steer_command_injected_at_reasoning_boundary() -> None:
    await reset_engine()
    await init_db()
    try:
        await _enqueue_steer(_CHAT_ID, "inject me")
        agent, middleware = _stub_agent(run_context=_run_context())

        events = [
            event
            async for event in middleware.on_reasoning(
                agent=agent,
                input_kwargs={},
                next_handler=_downstream(),
            )
        ]
        assert [event.hint for event in events] == ["inject me"]

        # take_steers marks the command done, so a later boundary sees nothing.
        assert await _steer_count(_CHAT_ID) == 0
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_executor_stop_leaves_pending_steer_undelivered() -> None:
    """A cancelled run never injects the steer it was holding.

    ``SteerMiddleware.steer()`` and ``AgentExecutor.cancel_steer()`` are gone:
    a steer is a ``chat_commands`` row taken at the ``on_reasoning`` boundary,
    and the executor's cancellation entry point is now ``stop()``. A steer
    queued while the run is parked must not be delivered, and stays waiting for
    the chat-end cleanup instead of hanging a waiter.
    """
    await reset_engine()
    await init_db()
    task: asyncio.Task[None] | None = None
    try:
        model = BlockingThenTextModel()
        middleware = SteerMiddleware(_run_context())
        agent = QwenPawDataAgent(
            name="qwenpaw-data",
            system_prompt="",
            model=model,
            toolkit=Toolkit(tools=[]),
            middlewares=[middleware],
            react_config=ReActConfig(max_iters=10),
        )
        executor = AgentExecutor(_run_context())
        envelope = FakeEnvelope()
        task = executor.start(
            agent,
            TurnInput(chat_id=_CHAT_ID, user_input="hello"),
            envelope,  # type: ignore[arg-type]
        )
        await asyncio.wait_for(model.first_call_started.wait(), timeout=5)

        await _enqueue_steer(_CHAT_ID, "cancel me")
        await executor.stop()

        # stop() released the parked run: the model only leaves its first call
        # through cancellation, since ``continue_first_call`` is never set.
        assert task.done()
        assert not model.continue_first_call.is_set()
        # Never injected: no steer hint reached the agent's stream.
        assert [
            event
            for event in envelope.events
            if getattr(event, "source", None) == "steer"
        ] == []
        # Still waiting, i.e. the cancelled turn did not consume or deliver it;
        # ChatRuntime._finish drops it with the chat.
        assert await _steer_count(_CHAT_ID) == 1
    finally:
        # If an assertion above fails before stop(), the run is still parked in
        # the model's first call; leaving it pending surfaces as "Task was
        # destroyed but it is pending" at teardown and masks the real failure.
        # Mirror _cleanup_runtime and cancel it before disposing the engine.
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await reset_engine()


@pytest.mark.asyncio
async def test_executor_stop_without_steer_middleware_is_noop() -> None:
    agent = QwenPawDataAgent(
        name="qwenpaw-data",
        system_prompt="",
        model=_StubModel(),
        toolkit=Toolkit(tools=[]),
        react_config=ReActConfig(max_iters=10),
    )
    executor = AgentExecutor(_run_context())
    executor._agent = agent
    await executor.stop()


def test_steer_api_200_and_409_chat_ended() -> None:
    asyncio.run(reset_engine())
    reset_hub()

    async def seed() -> tuple[str, str]:
        await init_db()
        session_factory = get_session_factory()
        async with session_factory() as db:
            session = Session.create(identity=TEST_IDENTITY, title="steer api")
            await SessionRepository(db, TEST_IDENTITY).add(session)
            chat = session.open_chat(
                text="hi",
                datasource_id="ds",
                has_active_chat=False,
            )
            await SessionRepository(db, TEST_IDENTITY).save(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
            return session.id, chat.id

    session_id, chat_id = asyncio.run(seed())
    app = create_app()
    path = f"/api/sessions/{session_id}/chats/{chat_id}/steer"

    async def _take_steers() -> list[dict[str, Any]]:
        async with get_session_factory()() as db:
            return await ChatCommandRepository(db).take_steers(chat_id)

    async def _mark_completed() -> None:
        async with get_session_factory()() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            chat.mark_status("completed")
            await ChatRepository(db, TEST_IDENTITY).save(chat)
            await db.commit()

    comments = [
        {
            "path": "russia_dau_mom.sql",
            "line_start": 2,
            "line_end": 10,
            "comment": "加一个参数",
        }
    ]

    with TestClient(app) as client:
        resp = client.post(path, json={"text": "api steer"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        resp = client.post(
            path,
            json={"text": "steer with comments", "artifact_comments": comments},
        )
        assert resp.status_code == 200

        # The router persists a Steer command instead of calling an in-process
        # runtime; both requests are queued in order with their comments.
        assert asyncio.run(_take_steers()) == [
            {"text": "api steer", "artifact_comments": []},
            {"text": "steer with comments", "artifact_comments": comments},
        ]

        asyncio.run(_mark_completed())
        ended = client.post(path, json={"text": "too late"})
        assert ended.status_code == 409
        body = ended.json()
        assert body["code"] == "CONFLICT"
        assert body["details"]["reason"] == "CHAT_ENDED"

    asyncio.run(reset_engine())


@pytest.mark.asyncio
async def test_snapshot_bundle_has_no_steers_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_engine()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    await init_db()
    session_factory = get_session_factory()

    from qwenpaw_data.host.core.api.mappers import chat_bundle_to_schema
    from qwenpaw_data.host.core.repositories.snapshot import SnapshotRepository

    async with session_factory() as db:
        session = Session.create(identity=TEST_IDENTITY, title="snap")
        await SessionRepository(db, TEST_IDENTITY).add(session)
        chat = session.open_chat(
            text="hi",
            datasource_id="ds",
            has_active_chat=False,
        )
        await SessionRepository(db, TEST_IDENTITY).save(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
        bundle = await SnapshotRepository(db, TEST_IDENTITY).bundle_chat(chat)
        schema = chat_bundle_to_schema(bundle).model_dump(mode="json")

    assert "steers" not in schema
    assert not hasattr(bundle, "steers")
    await reset_engine()
