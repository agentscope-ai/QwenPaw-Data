# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from qwenpaw_data.host.core.domain.chat_failure import ChatFailure
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths

from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.utils.time import utcnow


class FakeDb:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def commit(self) -> None:
        self.calls.append("commit")


class FakeSessionFactory:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __call__(self) -> FakeSessionFactory:
        return self

    async def __aenter__(self) -> FakeDb:
        return FakeDb(self.calls)

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakeEnvelope:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.stream = self

    async def flush(self) -> None:
        self.calls.append("flush")

    async def finalize(self) -> None:
        self.calls.append("finalize")

    async def emit_completed(self) -> None:
        self.calls.append("emit_completed")

    async def emit_cancelled(self) -> None:
        self.calls.append("emit_cancelled")


class FakeChat:
    id = "chat-1"
    session_id = "session-1"
    identity = Identity(user_id="user-1", tenant_id="tenant-1", workspace_id="ws-1")
    completed_at = None
    started_at = utcnow()
    last_sequence_number = -1
    plan = None
    agent_state = None

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def mark_status(self, status: str) -> None:
        self.calls.append(f"status:{status}")

    def mark_failed(self, failure: Any) -> None:
        self.calls.append("status:failed")

    def cancel(self) -> None:
        self.calls.append("status:canceled")


def _patch_finish(monkeypatch, calls: list[str]) -> None:
    class FakeChatRepository:
        def __init__(self, db: Any, identity: Any) -> None:
            pass

        async def save(self, _chat: Any) -> None:
            calls.append("save")

    class FakeCommandRepository:
        def __init__(self, db: Any) -> None:
            pass

        async def clear(self, _chat_id: str) -> None:
            calls.append("clear")

    async def score(*_args: Any, **_kwargs: Any) -> None:
        calls.append("score")

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.ChatRepository", FakeChatRepository
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.ChatCommandRepository",
        FakeCommandRepository,
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.tracer_send_status", score
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.tracer_send_stop", score
    )


async def test_completed_persists_terminal_status_before_response_event(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _patch_finish(monkeypatch, calls)
    runtime = ChatRuntime(FakeSessionFactory(calls), "chat-1")  # type: ignore[arg-type]
    await runtime._finish(
        FakeChat(calls),  # type: ignore[arg-type]
        None,
        FakeEnvelope(calls),  # type: ignore[arg-type]
        "completed",
    )

    assert calls == [
        "finalize",
        "status:completed",
        "save",
        "clear",
        "score",
        "commit",
        "emit_completed",
        "flush",
    ]


async def test_failed_with_no_envelope_still_marks_failed(monkeypatch) -> None:
    """初始化失败也要落库并通知用户，避免会话卡在 running。"""
    calls: list[str] = []
    _patch_finish(monkeypatch, calls)
    stream = SimpleNamespace(response_failed=AsyncMock())
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.OutputStream", Mock(return_value=stream)
    )
    failure = ChatFailure(
        "VALIDATION", "active models are not configured", "no active models"
    )

    runtime = ChatRuntime(FakeSessionFactory(calls), "chat-1")  # type: ignore[arg-type]
    await runtime._finish(
        FakeChat(calls),  # type: ignore[arg-type]
        None,
        None,
        "failed",
        failure=failure,
    )

    stream.response_failed.assert_awaited_once_with(error=failure.to_dict())
    assert calls == ["status:failed", "save", "clear", "score", "commit"]


def _submitted_ask_user_agent() -> SimpleNamespace:
    from agentscope.message import AssistantMsg, ToolCallBlock, ToolCallState
    from agentscope.permission import PermissionContext, PermissionMode
    from agentscope.state import AgentState

    state = AgentState(permission_context=PermissionContext(mode=PermissionMode.BYPASS))
    state.reply_id = "reply-1"
    state.context = [
        AssistantMsg(
            id="reply-1",
            name="QwenPawData",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="ask_user_question",
                    input='{"title":"Q","questions":[]}',
                    state=ToolCallState.SUBMITTED,
                ),
            ],
        ),
    ]
    return SimpleNamespace(name="QwenPawData", state=state)


async def test_finish_closes_submitted_tool_calls_before_saving_state(
    monkeypatch,
) -> None:
    from agentscope.message import ToolCallState, ToolResultState
    from agentscope.state import AgentState

    calls: list[str] = []
    saved: list[AgentState] = []

    class FakeStateStore:
        def __init__(self, db: Any, identity: Any) -> None:
            pass

        async def save(self, session_id: str, state: AgentState) -> None:
            assert session_id == "session-1"
            saved.append(state)
            calls.append("state")

    agent = _submitted_ask_user_agent()
    _patch_finish(monkeypatch, calls)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.AgentStateStore", FakeStateStore
    )

    runtime = ChatRuntime(FakeSessionFactory(calls), "chat-1")  # type: ignore[arg-type]
    await runtime._finish(
        FakeChat(calls),  # type: ignore[arg-type]
        agent,  # type: ignore[arg-type]
        FakeEnvelope(calls),  # type: ignore[arg-type]
        "canceled",
    )

    assert saved, "agent state must be persisted"
    persisted = saved[0]
    assert persisted.get_awaiting_tool_calls("QwenPawData") == []
    last = persisted.context[-1]
    tool_call = last.get_content_blocks("tool_call")[0]
    tool_result = last.get_content_blocks("tool_result")[0]
    assert tool_call.state == ToolCallState.FINISHED
    assert tool_result.id == "call-1"
    assert tool_result.state == ToolResultState.ERROR
    assert calls[:3] == ["finalize", "status:canceled", "state"]
    # canceled 路径写两条 score：stop 标记 + chat_status。
    assert calls.count("score") == 2
    assert calls == [
        "finalize",
        "status:canceled",
        "state",
        "save",
        "clear",
        "score",
        "score",
        "commit",
        "emit_cancelled",
        "flush",
    ]


async def test_run_closes_loaded_submitted_tool_calls_before_start(
    workspace_chat, monkeypatch
) -> None:
    from agentscope.message import ToolCallState

    case = workspace_chat
    agent = _submitted_ask_user_agent()
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.get_agent_builder",
        lambda _agent_id: SimpleNamespace(build=AsyncMock(return_value=agent)),
    )
    seen: list[Any] = []

    async def start(started, *_args, **_kwargs):
        seen.append(started)

    case.executor.start = start
    await case.runtime.run(identity=case.identity)

    assert seen
    started = seen[0]
    assert started.state.get_awaiting_tool_calls(started.name) == []
    tool_call = started.state.context[-1].get_content_blocks("tool_call")[0]
    tool_result = started.state.context[-1].get_content_blocks("tool_result")[0]
    assert tool_call.state == ToolCallState.FINISHED
    assert tool_result.id == "call-1"


@pytest.fixture
async def workspace_chat(tmp_path, monkeypatch):
    from agentscope.state import AgentState
    from qwenpaw_data.host.core.db.engine import init_db, reset_engine, get_session_factory
    from qwenpaw_data.host.core.domain.session import Session
    from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
    from qwenpaw_data.host.core.settings import clear_settings
    from qwenpaw_data.host.core.workspace import QwenPawDataWorkspaceManager

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test")
    clear_settings()
    await reset_engine()
    await init_db()
    factory = get_session_factory()
    identity = Identity(tenant_id="t", workspace_id="w", user_id="u")
    async with factory() as db:
        session = Session.create(identity=identity, agent_id="agent", title="renewal")
        chat = session.open_chat(text="test", datasource_id=None, has_active_chat=False)
        await SessionRepository(db, identity).add(session)
        await ChatRepository(db, identity).add(chat)
        await db.commit()

    manager = QwenPawDataWorkspaceManager()
    workspace = SimpleNamespace(
        workspace_id="test",
        is_alive=True,
        renew=AsyncMock(),
        close=AsyncMock(),
    )
    manager._create_workspace = AsyncMock(return_value=workspace)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.get_workspace_manager", lambda: manager
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.PreferencesRepository.load",
        AsyncMock(return_value=SimpleNamespace(resolve=lambda: None)),
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        AsyncMock(return_value=[]),
    )
    executor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.AgentExecutor",
        lambda _ctx: executor,
    )
    runtime = ChatRuntime(factory, chat.id)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.get_agent_builder",
        lambda agent_id: SimpleNamespace(
            build=AsyncMock(return_value=SimpleNamespace(state=AgentState()))
        ),
    )
    monkeypatch.setattr(runtime, "_log_chat_start", lambda *args: None)
    paths = UserAgentPaths(tmp_path / "home", "t", "w", "u", "agent", session.id)
    try:
        yield SimpleNamespace(
            runtime=runtime,
            executor=executor,
            manager=manager,
            workspace=workspace,
            factory=factory,
            chat=chat,
            session=session,
            identity=identity,
            paths=paths,
        )
    finally:
        await manager.stop()
        await reset_engine()


async def test_workspace_retirement_during_save_preserves_completed_status(
    workspace_chat, monkeypatch
):
    from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
    from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore

    case = workspace_chat
    reached, release = asyncio.Event(), asyncio.Event()
    save = AgentStateStore.save

    async def blocked_save(self, *args, **kwargs):
        reached.set()
        await release.wait()
        await save(self, *args, **kwargs)

    monkeypatch.setattr(AgentStateStore, "save", blocked_save)

    task = asyncio.create_task(case.runtime.run(identity=case.identity))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        case.workspace.renew.side_effect = RuntimeError("renewal failed")
        # Another session's acquisition retires this same workspace.
        with pytest.raises(RuntimeError, match="renewal failed"):
            async with case.manager.lease(replace(case.paths, session_id="other")):
                pass
        release.set()
        await asyncio.wait_for(task, 2)
        async with case.factory() as db:
            assert (
                await ChatRepository(db, case.identity).get(case.chat.id)
            ).status == "completed"
            assert not await SessionRepository(db, case.identity).has_active_chat(
                case.session.id
            )
        assert case.runtime._finished.is_set()
        case.workspace.close.assert_awaited_once()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_workspace_renewal_failure_does_not_cancel_chat(workspace_chat):
    from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository

    case = workspace_chat
    reached, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        reached.set()
        await release.wait()

    case.executor.start = blocked
    task = asyncio.create_task(case.runtime.run(identity=case.identity))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        case.workspace.renew.side_effect = PermissionError("FC renewal access denied")
        case.manager._schedule_sweep()
        await asyncio.wait_for(asyncio.gather(*case.manager._sweep_jobs.values()), 2)
        assert not task.done()
        case.workspace.close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(task, 2)
        async with case.factory() as db:
            saved = await ChatRepository(db, case.identity).get(case.chat.id)
            assert saved.status == "completed"
            assert saved.error is None
            assert not await SessionRepository(db, case.identity).has_active_chat(
                case.session.id
            )
        case.workspace.close.assert_awaited_once()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_mcp_headers_synced_before_cm_runtime_headers(
    workspace_chat, monkeypatch
):
    from agentscope.mcp import MCPClient

    from qwenpaw_data.host.core.repositories import ChatRepository

    case = workspace_chat
    client = MCPClient(
        name="context-manager",
        is_stateful=False,
        mcp_config={"type": "http_mcp", "url": "http://cm/mcp"},
    )
    case.workspace.list_mcps = AsyncMock(return_value=[client])
    headers = {
        "context-manager": {
            "X-DataAgent-Service-Id": "cm-service-123",
            "chat_id": "external-chat",
        }
    }

    async def sync_headers(self, workspace, **kwargs):
        from qwenpaw_data.host.core.integrations.cm import set_cm_mcp_headers

        workspace.close.assert_not_awaited()
        assert kwargs["mcp_headers"] == headers
        assert kwargs["access_token"] == "host-token"
        client.mcp_config.headers = dict(headers[client.name])
        await set_cm_mcp_headers(
            client,
            kwargs["access_token"],
            session_id=kwargs["session_id"],
            chat_id=kwargs["request_id"],
        )

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        sync_headers,
    )

    async def execute(*args, **kwargs):
        case.workspace.close.assert_not_awaited()
        assert client.mcp_config.headers == headers[client.name]
        assert client._runtime_headers == {
            "Authorization": "Bearer host-token",
            "session_id": case.session.id,
            "chat_id": case.chat.id,
        }

    case.executor.start = AsyncMock(side_effect=execute)
    await case.runtime.run(
        identity=case.identity,
        access_token="host-token",
        mcp_headers=headers,
    )

    case.executor.start.assert_awaited_once()
    # Runtime catches executor errors, including assertions in execute().
    async with case.factory() as db:
        assert (
            await ChatRepository(db, case.identity).get(case.chat.id)
        ).status == "completed"


async def test_iteration_limit_persists_failure_and_replays_one_failed_response(
    workspace_chat,
):
    from agentscope.event import TextBlockDeltaEvent, TextBlockStartEvent
    from qwenpaw_data.host.core.repositories import ChatRepository
    from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
    from qwenpaw_data.host.core.domain.exception import IterationLimitExceeded

    case = workspace_chat

    async def execute(_agent, _turn, envelope, **_kwargs):
        await envelope.translate_event(
            TextBlockStartEvent(reply_id="reply-limit", block_id="text-1")
        )
        await envelope.translate_event(
            TextBlockDeltaEvent(
                reply_id="reply-limit", block_id="text-1", delta="partial answer"
            )
        )
        raise IterationLimitExceeded("qwenpaw-data")

    case.executor.start = AsyncMock(side_effect=execute)
    await case.runtime.run(identity=case.identity)

    async with case.factory() as db:
        saved = await ChatRepository(db, case.identity).get(case.chat.id)
        events = await ChatEventRepository(db, case.identity).read_after(
            case.chat.id, -1
        )
    assert saved.status == "failed"
    assert saved.error is not None
    assert saved.error["code"] == "REACT_MAX_ITERS"
    terminal = [
        event
        for event in events
        if event.object == "response"
        and event.status in {"completed", "failed", "cancelled"}
    ]
    assert len(terminal) == 1
    assert terminal[0].status == "failed"
    assert terminal[0].error.model_dump() == saved.error
    messages = [
        event
        for event in events
        if event.object == "message" and event.status == "completed"
    ]
    assert len(messages) == 1
    assert messages[0].content[0].text == "partial answer"
    assert "[Warning]" not in str(events)
