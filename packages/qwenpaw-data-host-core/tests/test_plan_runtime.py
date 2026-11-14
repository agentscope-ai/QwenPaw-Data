# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultState,
)
from agentscope.formatter import DashScopeChatFormatter
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState

from qwenpaw_data.host.core.utils.agent import close_interrupted_tool_calls
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
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatEventRepository,
    ChatRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class BlockingThenTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "blocking-plan-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()
        self.first_call_started = asyncio.Event()
        self.continue_first_call = asyncio.Event()
        self.messages: list[list[Any]] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, tools, tool_choice, kwargs)
        self.messages.append(list(messages))
        if len(self.messages) == 1:
            self.first_call_started.set()
            await self.continue_first_call.wait()
        return ChatResponse(
            content=[TextBlock(text="done")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def test_interrupted_tool_calls_are_closed_before_cancel_finish() -> None:
    state = AgentState(
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    state.reply_id = "reply-1"
    state.context = [
        AssistantMsg(
            id="reply-1",
            name="QwenPawData",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="PlanUpdate",
                    input='{"changes":[]}',
                    state=ToolCallState.ALLOWED,
                ),
            ],
        ),
    ]
    agent = SimpleNamespace(name="QwenPawData", state=state)

    close_interrupted_tool_calls(agent)

    message = state.context[-1]
    tool_call = message.get_content_blocks("tool_call")[0]
    tool_result = message.get_content_blocks("tool_result")[0]
    assert tool_call.state == ToolCallState.FINISHED
    assert tool_result.id == tool_call.id
    assert tool_result.state == ToolResultState.ERROR


@pytest.mark.asyncio
async def test_active_chat_plan_edit_hot_replaces_without_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    await reset_engine()
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))

    # Plan 热更新单测不关心 BizTrace；真实 Transformer.join 会等 flush，拖垮 5s 超时。
    class _NoopTransformer:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            return None

        async def append(self, entry: Any, *, last: bool = False) -> None:
            return None

        async def join(self) -> None:
            return None

    monkeypatch.setattr(
        "qwenpaw_data.host.core.agent.middleware.trace_forward.BizTraceTransformer",
        _NoopTransformer,
    )

    class _NoopFollowUp:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            return None

        async def append(self, entry: Any, *, last: bool = False) -> None:
            return None

        async def join(self) -> list[str]:
            return []

    monkeypatch.setattr(
        "qwenpaw_data.host.core.agent.middleware.followup.FollowUpRecommend",
        _NoopFollowUp,
    )
    await init_db()
    session_factory = get_session_factory()

    async with session_factory() as db:
        session = Session.create(
            identity=TEST_IDENTITY,
            title="runtime plan",
        )
        session_repo = SessionRepository(db, TEST_IDENTITY)
        chat_repo = ChatRepository(db, TEST_IDENTITY)
        await session_repo.add(session)
        chat = session.open_chat(
            text="start",
            datasource_id="ds",
            has_active_chat=False,
        )
        await session_repo.save(session)
        await chat_repo.add(chat)
        await db.commit()

    await seed_test_preferences(session_factory)
    model = BlockingThenTextModel()
    monkeypatch.setattr(builder_module, "build_model", lambda *_a, **_k: model)

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        AsyncMock(return_value=[]),
    )
    runtime = ChatRuntime(session_factory, chat.id)
    run_task = asyncio.create_task(runtime.run(identity=TEST_IDENTITY))

    try:
        await asyncio.wait_for(model.first_call_started.wait(), timeout=5)
        # Editing the plan of an active chat enqueues a ChatCommand (the router
        # never touches the runtime in-process); the running ChatRuntime's plan
        # watcher applies it in place without interrupting the turn.
        async with session_factory() as db:
            active = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
            await ChatCommandRepository(db).add(
                ChatCommand.for_plan(
                    active,
                    reason="new priority",
                    plan={
                        "tasks": [
                            {
                                "id": "1",
                                "subject": "Replacement",
                                "description": "Continue with replacement",
                            },
                        ],
                    },
                )
            )
            await db.commit()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while [task.id for task in runtime.agent.get_plan().tasks] != [
            "1"
        ] and loop.time() < deadline:
            await asyncio.sleep(0.01)
        context = runtime.agent.get_plan()
        assert [task.id for task in context.tasks] == ["1"]
        assert any(
            "修改原因：new priority" in str(msg) for msg in runtime.agent.state.context
        )

        model.continue_first_call.set()
        await asyncio.wait_for(run_task, timeout=5)

        async with session_factory() as db:
            saved_chat = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
            saved_state = await AgentStateStore(db, TEST_IDENTITY).load(session.id)
            events = await ChatEventRepository(db, TEST_IDENTITY).read_after(
                chat.id, -1
            )

        assert saved_chat.status == "completed"
        assert [task.id for task in saved_chat.plan.tasks] == ["1"]
        assert saved_state.tasks_context.tasks == []
        seqs = [event.sequence_number for event in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
        assert {event.object for event in events} <= {
            "message",
            "response",
            "error",
            "context.usage",
            "model.call",
            "model.call.end",
            "tool.result.end",
        }
        response_statuses = [
            event.status for event in events if event.object == "response"
        ]
        assert response_statuses == ["created", "in_progress", "completed"]
    finally:
        if not run_task.done():
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        reset_hub()
        await reset_engine()
