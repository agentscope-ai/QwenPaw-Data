from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from agentscope.agent._config import ReActConfig
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.db.tables import ModelUsageRow
from qwenpaw_data.host.core.providers.usage import attach_model_usage
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
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
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


class DelegatingModel(FakeTextModel):
    """Real AgentScope tool dispatch and nested model calls, no external model."""

    def __init__(self, role: str, artifact_dir: Path):
        super().__init__()
        self.role = role
        self.artifact_dir = artifact_dir
        self.artifacts = (
            [{"path": "report.html", "name": "Report"}] if role == "report" else []
        )
        self.delegated = False
        self.child_calls = 0

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        schemas = {schema["function"]["name"] for schema in tools or []}
        if "spawn_subagent" not in schemas:
            self.messages = list(messages)
            self.child_calls += 1
            assert PLAN_TOOL_NAMES.isdisjoint(schemas)
            assert "ask_user_question" not in schemas
            assert "ds-console" in str(messages)
            if self.role != "data_fetcher":
                assert "submit_result" in schemas
                if self.role == "workspace_reader":
                    assert "Read" in schemas and "Write" not in schemas
                    assert "Read only the supplied evidence." in str(messages)
                if self.role != "general":
                    assert not any(name.startswith("mcp__") for name in schemas)
                if self.role == "report" and self.child_calls == 1:
                    return ChatResponse(
                        content=[
                            ToolCallBlock(
                                id="write-report",
                                name="Write",
                                input=json.dumps(
                                    {
                                        "file_path": (
                                            self.artifact_dir / "report.html"
                                        ).as_posix(),
                                        "content": "<h1>Scoped report</h1>",
                                    }
                                ),
                            )
                        ],
                        is_last=True,
                        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
                    )
                return ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="handoff",
                            name="submit_result",
                            input=json.dumps(
                                {
                                    "summary": "CHILD_VERIFIED",
                                    "artifacts": self.artifacts,
                                }
                            ),
                        )
                    ],
                    is_last=True,
                    usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
                )
            return ChatResponse(
                content=[TextBlock(text="CHILD_VERIFIED")],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )
        if not self.delegated:
            self.delegated = True
            roles = ["report", "general"] if self.role == "both" else [self.role]
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"delegate-{role}",
                        name="spawn_subagent",
                        input=(
                            '{"task":"Summarize the supplied context without querying data"}'
                            if role == "data_fetcher"
                            else json.dumps(
                                {
                                    "type": role,
                                    "description": "Read evidence",
                                    "prompt": "Summarize the supplied context.",
                                }
                            )
                        ),
                    )
                    for role in roles
                ],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )
        assert "CHILD_VERIFIED" in str(messages)
        if self.role not in {"data_fetcher", "both"}:
            receipt = next(
                block
                for message in messages
                for block in message.content
                if isinstance(block, ToolResultBlock) and block.name == "spawn_subagent"
            )
            assert json.loads("".join(b.text for b in receipt.output)) == {
                "status": "completed",
                "summary": "CHILD_VERIFIED",
                "artifacts": self.artifacts,
            }
        return await super()._call_api(
            model_name, messages, tools, tool_choice, **kwargs
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


def test_data_agent_parallel_setting_can_be_disabled(monkeypatch):
    from qwenpaw_data.host.core.settings import get_settings

    monkeypatch.setenv("QWENPAW_DATA_TOOL_PARALLEL_ENABLED", "false")
    clear_settings()
    try:
        assert get_settings().tools.parallel_enabled is False
    finally:
        clear_settings()


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
    assert "spawn_subagent" in tool_names
    assert "trigger_review_agent" not in tool_names
    assert "mcp__fake-mcp__execute_sql" in tool_names
    assert "reset_tools" not in tool_names
    spawn = await toolkit.get_tool("spawn_subagent")
    assert spawn is not None and spawn.is_concurrency_safe is True
    for name in ("ask_user_question", "PlanCreate", "CronJob"):
        tool = await toolkit.get_tool(name)
        assert tool is not None and tool.is_concurrency_safe is False


@pytest.mark.parametrize("enabled", ["0", "1"])
async def test_production_builder_subagent_switch(monkeypatch, enabled):
    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", enabled)
    toolkit = await DataAgentBuilder().build_toolkit(
        run_context=_run_context(FakeWorkspace())
    )
    names = {schema["function"]["name"] for schema in await toolkit.get_tool_schemas()}
    assert ("spawn_subagent" in names) is (enabled == "1")


async def test_subagent_inherits_enabled_skills_and_bound_mcp_clients(monkeypatch):
    from qwenpaw_data.host.core.agent.spawn_subagent import SpawnSubagent

    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", "1")
    workspace = FakeWorkspace()
    workspace.list_skills = AsyncMock(return_value=[SimpleNamespace(name="fetch-data")])
    workspace.list_mcps = AsyncMock(return_value=[FakeMCP()])
    context = _run_context(workspace)
    context.disabled_skills = frozenset({"fetch-data"})
    toolkit = await DataAgentBuilder().build_toolkit(run_context=context)
    child = next(
        tool
        for group in toolkit.tool_groups
        for tool in group.tools
        if isinstance(tool, SpawnSubagent)
    )
    assert child._ctx is context
    assert child._workspace_skills == []
    assert child._workspace_mcps == workspace.list_mcps.return_value
    workspace.list_mcps.assert_awaited_once_with(
        agent_id="agent-a", session_id="session-a"
    )


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
@pytest.mark.parametrize(
    "delegate", [None, "data_fetcher", "report", "workspace_reader", "both"]
)
async def test_chat_runtime_uses_qwenpaw_data_agent_with_plan_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
    delegate,
) -> None:
    await reset_engine()
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QWENPAW_DATA_TOOL_PARALLEL_ENABLED", raising=False)
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
    paths = UserAgentPaths.from_identity(
        home=tmp_path / "home",
        identity=TEST_IDENTITY,
        agent_id="agent-a",
        session_id=session.id,
    )
    if delegate == "workspace_reader":
        role_dir = paths.workspace / "agents" / paths.agent_id
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "workspace_reader.md").write_text(
            "---\ndescription: Read evidence\ntools: [Read]\nskills: []\nmcp: []\n---\n"
            "Read only the supplied evidence.",
            encoding="utf-8",
        )
    model = (
        DelegatingModel(delegate, paths.artifact_dir) if delegate else FakeTextModel()
    )
    seen = {}
    # Exercise the SDK's actual injected prompt on either side of Shanghai midnight.
    instant = (
        datetime(2026, 4, 5, 15, 59, tzinfo=UTC)
        if delegate == "report"
        else datetime(2026, 4, 5, 16, 1, tzinfo=UTC)
    )
    expected_clock = "2026-04-05T23:59" if delegate == "report" else "2026-04-06T00:01"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                instant.astimezone(tz)
                if tz is not None
                else instant.replace(tzinfo=None)
            )

    monkeypatch.setattr("agentscope.agent._agent.datetime", Clock)
    child_models = []

    def build_fixture(*_args, **kwargs):
        seen.update(kwargs)
        step = kwargs["usage"].source_step
        fixture = model
        if step:
            fixture = DelegatingModel(
                step.removeprefix("subagent:"), paths.artifact_dir
            )
            child_models.append(fixture)
        return attach_model_usage(fixture, kwargs["usage"], provider_id="fixture")

    monkeypatch.setattr(builder_module, "build_model", build_fixture)

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
        prompt_text = "\n".join(
            message.get_text_content() or "" for message in model.messages
        )
        assert expected_artifact_dir.as_posix() in prompt_text.replace("\\", "/")
        assert "local workspace" in prompt_text
        assert "1 [pending] Persisted task" in prompt_text
        assert "Name:" not in prompt_text
        assert not (tmp_path / "home" / "sessions").exists()
        assert PLAN_TOOL_NAMES <= model.tool_names
        assert seen["parallel_tool_calls"] is True
        if delegate:
            assert model.delegated
            for fixture in [model, *child_models]:
                assert expected_clock in str(fixture.messages)
        if delegate == "report":
            assert (
                expected_artifact_dir / "report.html"
            ).read_text() == "<h1>Scoped report</h1>"

        async with session_factory() as db:
            usage = list(
                (
                    await db.scalars(
                        select(ModelUsageRow).where(ModelUsageRow.chat_id == chat.id)
                    )
                ).all()
            )
            expected_steps = ["", ""] if delegate else [""]
            if delegate == "both":
                expected_steps.extend(
                    ["subagent:general", "subagent:report", "subagent:report"]
                )
            elif delegate:
                expected_steps.extend(
                    [f"subagent:{delegate}"] * (2 if delegate == "report" else 1)
                )
            assert sorted(row.source_step for row in usage) == expected_steps
            assert all(
                row.session_id == session.id and row.input_tokens == 1 for row in usage
            )
            saved_chat = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
            assert saved_chat.status == "completed"
            assert saved_chat.plan.tasks[0].id == "1"
            saved_state = await AgentStateStore(db, TEST_IDENTITY).load(session.id)
            assert isinstance(saved_state, AgentState)
            assert saved_state.tasks_context.tasks == []
            if delegate:
                events = await ChatEventRepository(db, TEST_IDENTITY).read_after(
                    chat.id, -1
                )
                # Verify durable parent-visible output, not only the fake model's calls.
                assert "CHILD_VERIFIED" in str([event.model_dump() for event in events])
                children = [e for e in events if e.stream.type == "subagent"]
                roles = {"report", "general"} if delegate == "both" else {delegate}
                assert {e.stream.parent_call_id for e in children} == {
                    f"delegate-{r}" for r in roles
                }
                assert (
                    children[-1].object == "response"
                    and children[-1].status == "completed"
                )
                assert (
                    events[-1].stream.type == "main"
                    and events[-1].status == "completed"
                )
    finally:
        await stop_workspace_manager()
        reset_hub()
        await reset_engine()
