from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.agent._config import ReActConfig
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.tools.plan_tools import PLAN_TOOL_NAMES, PlanCreate
from qwenpaw_data.host.core.runtime.builder import DataAgentBuilder
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.ui import UI_TOOL_NAMES
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class FakeTool:
    description = "fake"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(self, name: str, *, is_mcp: bool = False) -> None:
        self.name = name
        self.is_mcp = is_mcp
        self.mcp_name = "fake-mcp" if is_mcp else None


class FakeMCP:
    name = "fake-mcp"
    is_stateful = False
    is_connected = False
    mcp_config = {
        "type": "http_mcp",
        "url": "http://example.test/mcp",
        "headers": {},
        "timeout": 30.0,
    }
    execution_timeout = None

    async def list_tools(self) -> list[FakeTool]:
        return [FakeTool("mcp__fake-mcp__execute_sql", is_mcp=True)]


class FakeWorkspace:
    async def list_tools(self) -> list[FakeTool]:
        return [FakeTool("Read"), FakeTool("Write"), FakeTool("Bash")]

    async def list_mcps(self, *, agent_id: str, session_id: str) -> list[FakeMCP]:
        return [FakeMCP()]

    async def list_skills(self, *, agent_id: str) -> list:
        return []


def _run_context(workspace: Any) -> RunContext:
    return RunContext(
        session_id="session-a",
        chat_id="chat-a",
        workspace=workspace,
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=TEST_TENANT_ID,
            workspace_id=TEST_WORKSPACE_ID,
            user_id=TEST_USER_ID,
            agent_id="agent-a",
            session_id="session-a",
        ),
        user_runtime_config=SimpleNamespace(  # type: ignore[arg-type]
            tool_timeout_enabled=True, tool_timeout_seconds=1800
        ),
        identity=TEST_IDENTITY,
        session_factory=get_session_factory(),
        request_context={"datasource_id": "ds_demo"},
    )


class FakeTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "fake-qwenpaw-data-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()
        self.tool_names: set[str] = set()
        self.messages: list[Any] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, tool_choice, kwargs)
        self.messages = list(messages)
        self.tool_names = {schema["function"]["name"] for schema in tools or []}
        return ChatResponse(
            content=[TextBlock(text="你好，我是 QwenPawData。")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def test_qwenpaw_data_agent_injects_shanghai_time() -> None:
    agent = QwenPawDataAgent(
        name="qwenpaw-data",
        system_prompt="",
        model=FakeTextModel(),
        toolkit=Toolkit(tools=[]),
        react_config=ReActConfig(max_iters=1),
    )
    assert agent.injection_config.timezone == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_build_qwenpaw_data_toolkit_registers_workspace_and_local_tools() -> None:
    toolkit = await DataAgentBuilder().build_toolkit(
        run_context=_run_context(FakeWorkspace())
    )

    tool_names = {
        schema["function"]["name"] for schema in await toolkit.get_tool_schemas()
    }

    assert {"Read", "Write", "Bash"} <= tool_names
    assert PLAN_TOOL_NAMES <= tool_names
    assert UI_TOOL_NAMES <= tool_names
    assert "cm_dataset_lookup" in tool_names
    assert "ask_user_question" in tool_names
    assert "CronJob" in tool_names
    assert "trigger_review_agent" not in tool_names
    assert "mcp__fake-mcp__execute_sql" in tool_names
    assert "reset_tools" not in tool_names


@pytest.mark.asyncio
async def test_build_qwenpaw_data_toolkit_omits_ui_tools_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_TOOL_UI_ENABLED", "false")
    clear_settings()
    toolkit = await DataAgentBuilder().build_toolkit(
        run_context=_run_context(FakeWorkspace())
    )
    tool_names = {
        schema["function"]["name"] for schema in await toolkit.get_tool_schemas()
    }
    assert tool_names.isdisjoint(UI_TOOL_NAMES)
    assert "cm_dataset_lookup" in tool_names


@pytest.mark.asyncio
async def test_chat_runtime_uses_qwenpaw_data_agent_with_plan_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    await reset_engine()
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    await init_db()
    session_factory = get_session_factory()

    async with session_factory() as db:
        session = Session.create(
            identity=TEST_IDENTITY,
            agent_id="agent-a",
            title="console",
        )
        session_repo = SessionRepository(db, TEST_IDENTITY)
        chat_repo = ChatRepository(db, TEST_IDENTITY)
        await session_repo.add(session)
        chat = session.open_chat(
            text="你好",
            datasource_id="ds-console",
            has_active_chat=False,
        )
        chat.plan = PlanCreate.build_context(
            [
                {
                    "id": "1",
                    "subject": "Persisted task",
                    "description": "Restored for this Chat",
                },
            ],
        )
        await session_repo.save(session)
        await chat_repo.add(chat)
        await db.commit()

    await seed_test_preferences(session_factory)
    model = FakeTextModel()
    monkeypatch.setattr(builder_module, "build_model", lambda *_a, **_k: model)

    # The builtin context-manager MCP is an external service; this test only
    # cares about the Host-side turn.
    monkeypatch.setattr(McpRepository, "sync", AsyncMock())
    from qwenpaw_data.host.core.workspace import (
        get_workspace_manager,
        stop_workspace_manager,
    )

    await get_workspace_manager().start()
    try:
        await ChatRuntime(session_factory, chat.id).run(identity=TEST_IDENTITY)

        expected_workspace = (
            tmp_path
            / "home"
            / TEST_TENANT_ID
            / TEST_WORKSPACE_ID
            / TEST_USER_ID
            / "agent-a"
            / "workspace"
        )
        expected_artifact_dir = expected_workspace / "artifacts" / session.id
        assert expected_artifact_dir.is_dir()
        assert str(expected_artifact_dir) in str(model.messages)
        assert "local workspace" in str(model.messages)
        assert "1 [pending] Persisted task" in str(model.messages)
        assert "Name:" not in str(model.messages)
        assert not (tmp_path / "home" / "sessions").exists()
        assert PLAN_TOOL_NAMES <= model.tool_names

        async with session_factory() as db:
            saved_chat = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
            assert saved_chat.status == "completed"
            assert saved_chat.plan.tasks[0].id == "1"
            saved_state = await AgentStateStore(db, TEST_IDENTITY).load(session.id)
            assert isinstance(saved_state, AgentState)
            assert saved_state.tasks_context.tasks == []
    finally:
        await stop_workspace_manager()
        reset_hub()
        await reset_engine()
