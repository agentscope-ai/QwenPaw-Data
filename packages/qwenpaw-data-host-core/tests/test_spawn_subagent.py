from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent
from agentscope.event import (
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.skill import Skill
from agentscope.state import AgentState

import qwenpaw_data.host.core.agent.spawn_subagent as spawn_module
from qwenpaw_data.host.core.agent.spawn_subagent import (
    SpawnSubagent,
    _SubAgent,
    _build_sub_prompt,
)
from qwenpaw_data.host.core.orchestration import RuntimeStateManager


def _chunk_text(chunk) -> str:
    return "".join(getattr(block, "text", "") for block in chunk.content)


async def _collect(tool: SpawnSubagent, **kwargs):
    return [chunk async for chunk in tool(**kwargs)]


async def _runtime_with_in_progress() -> RuntimeStateManager:
    rs = RuntimeStateManager()
    await rs.create_plan(
        name="Test",
        description="desc",
        expected_outcome="out",
        nodes=[
            {
                "node_id": "n0",
                "name": "upstream",
                "description": "d",
                "expected_outcome": "o",
            },
            {
                "node_id": "n1",
                "name": "current",
                "description": "d",
                "expected_outcome": "o",
                "deps": ["n0"],
            },
        ],
    )
    await rs.update_subtask(
        "n0",
        "done",
        reasoning="loaded",
        summary="upstream summary",
    )
    await rs.update_subtask("n1", "in_progress")
    return rs


class FakeWorkspaceTool:
    description = "fake"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_concurrency_safe = True
    is_read_only = False
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(self, name: str) -> None:
        self.name = name


class FakeMCPTool(FakeWorkspaceTool):
    is_mcp = True
    mcp_name = "context-manager"
    is_read_only = True


class FakeMCP:
    name = "context-manager"
    is_stateful = False
    is_connected = False

    async def list_tools(self) -> list[FakeMCPTool]:
        return [
            FakeMCPTool("mcp__context-manager__describe_metrics"),
            FakeMCPTool("mcp__context-manager__execute_sql"),
        ]


class FakeSubAgent:
    events = []
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeSubAgent.instances.append(self)

    def reply_stream(self, inputs=None):
        async def gen():
            for event in self.events:
                if isinstance(event, BaseException):
                    raise event
                yield event

        return gen()


def _make_tool(
    tmp_path,
    rs: RuntimeStateManager | None = None,
    *,
    request_context: dict | None = None,
) -> SpawnSubagent:
    workspace = SimpleNamespace(workdir=str(tmp_path / "workspace"))
    return SpawnSubagent(
        runtime_state=rs or RuntimeStateManager(),
        workspace=workspace,
        workspace_tools=[
            FakeWorkspaceTool("Bash"),
            FakeWorkspaceTool("Read"),
            FakeWorkspaceTool("Write"),
            FakeWorkspaceTool("download_file"),
        ],
        workspace_mcps=[FakeMCP()],
        workspace_skills=[
            Skill(
                name="fetch-data",
                description="fetch data",
                dir=str(tmp_path / "skills/fetch-data"),
                markdown="",
                updated_at=0,
            ),
            Skill(
                name="bi-report-generation",
                description="report",
                dir="skills/bi-report-generation",
                markdown="",
                updated_at=0,
            ),
        ],
        parent_agent_getter=lambda: SimpleNamespace(
            model=object(),
            offloader=workspace,
            _request_context=request_context or {},
            state=AgentState(
                permission_context=PermissionContext(
                    mode=PermissionMode.ACCEPT_EDITS,
                ),
            ),
        ),
        workspace_dir=tmp_path / "workspace",
        artifacts_root=tmp_path / "workspace" / "artifacts",
        session_id_getter=lambda: "s1",
        cm_mcp_tool_prefixes={"mcp__context-manager__"},
    )


def test_build_sub_prompt_includes_context_and_environment(tmp_path) -> None:
    prompt = _build_sub_prompt(
        "查询数据",
        "按天聚合",
        {"n0": "上游摘要"},
        ["Bash"],
        ["mcp__context-manager__execute_sql"],
        workspace_dir=tmp_path / "workspace",
        artifacts_root=tmp_path / "workspace" / "artifacts",
        session_id="s1",
        graph_id="graph_1",
        node_id="n1",
    )

    assert "查询数据" in prompt
    assert "按天聚合" in prompt
    assert "上游摘要" in prompt
    assert "Bash" in prompt
    assert "execute_sql" in prompt
    assert "artifacts/s1/graph_1/n1/" in prompt


def test_build_sub_prompt_uses_session_artifacts_without_active_node(
    tmp_path,
) -> None:
    prompt = _build_sub_prompt(
        "直接查询",
        "",
        {},
        ["Bash"],
        [],
        workspace_dir=tmp_path / "workspace",
        artifacts_root=tmp_path / "workspace" / "artifacts",
        session_id="s1",
    )

    assert "当前会话产物必须保存到 `artifacts/s1/`" in prompt
    assert "s1/<filename>" in prompt
    assert "<graph_id>" not in prompt
    assert "<node_id>" not in prompt


def test_master_prompt_gates_subagent(monkeypatch) -> None:
    from qwenpaw_data.host.core.prompts import (
        analysis_environment_hint,
        build_master_prompt,
    )

    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)
    agent_prompt = build_master_prompt(mode="agent")
    assert "spawn_subagent(task" in agent_prompt
    assert "没有活动 TaskGraph 节点时" in agent_prompt
    assert "spawn_subagent(task" not in build_master_prompt(mode="plan")
    environment = analysis_environment_hint(session_id="s1")
    assert "artifacts/s1/" in environment
    assert "不要臆造" in environment

    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", "0")
    assert "spawn_subagent(task" not in build_master_prompt(mode="agent")


@pytest.mark.asyncio
async def test_prompt_uses_runtime_context(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [
        Msg(
            name="sub",
            role="assistant",
            content=[TextBlock(type="text", text="done")],
        ),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    rs = await _runtime_with_in_progress()
    tool = _make_tool(tmp_path, rs)

    chunks = await _collect(tool, task="执行当前节点", context="用户约束")

    prompt = FakeSubAgent.instances[0].kwargs["system_prompt"]
    assert chunks[-1].is_last is True
    assert "执行当前节点" in prompt
    assert "用户约束" in prompt
    assert "upstream summary" in prompt
    assert "artifacts/s1/" in prompt
    assert "/n1/" in prompt


@pytest.mark.asyncio
async def test_role_injection_allowlists_tools_and_fetch_skill(
    monkeypatch,
    tmp_path,
) -> None:
    FakeSubAgent.events = [
        Msg(
            name="sub",
            role="assistant",
            content=[TextBlock(type="text", text="done")],
        ),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)

    await _collect(_make_tool(tmp_path), task="x")

    toolkit = FakeSubAgent.instances[0].kwargs["toolkit"]
    tool_names = {
        tool.name
        for group in toolkit.tool_groups
        for tool in getattr(group, "tools", [])
    }
    assert {"Bash", "Read", "Write"} <= tool_names
    assert "download_file" not in tool_names
    skill_names = {
        getattr(skill, "name", "")
        for group in toolkit.tool_groups
        for skill in getattr(group, "skills_or_loaders", [])
    }
    assert skill_names == {"fetch-data"}
    permission_context = FakeSubAgent.instances[0].kwargs["state"].permission_context
    assert permission_context.mode is PermissionMode.ACCEPT_EDITS


@pytest.mark.asyncio
async def test_unattended_subagent_confirmation_is_denied(
    monkeypatch,
    tmp_path,
) -> None:
    tool_call = ToolCallBlock(
        id="tc-confirm",
        name="Bash",
        input='{"command":"curl example.com"}',
    )

    class ConfirmingSubAgent(FakeSubAgent):
        received = []

        def reply_stream(self, inputs=None):
            self.received.append(inputs)

            async def gen():
                if isinstance(inputs, UserConfirmResultEvent):
                    yield Msg(
                        name="sub",
                        role="assistant",
                        content=[TextBlock(type="text", text="denied safely")],
                    )
                else:
                    yield RequireUserConfirmEvent(
                        reply_id="reply-confirm",
                        tool_calls=[tool_call],
                    )

            return gen()

    monkeypatch.setattr(spawn_module, "_SubAgent", ConfirmingSubAgent)

    chunks = await _collect(_make_tool(tmp_path), task="x")

    confirmation = ConfirmingSubAgent.received[1]
    assert isinstance(confirmation, UserConfirmResultEvent)
    assert confirmation.confirm_results[0].confirmed is False
    assert "denied safely" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_unsupported_role(tmp_path) -> None:
    chunks = await _collect(_make_tool(tmp_path), task="x", role="bad_role")

    assert len(chunks) == 1
    assert chunks[0].is_last is True
    assert "不支持" in _chunk_text(chunks[0])


@pytest.mark.asyncio
async def test_subagent_creation_failure(monkeypatch, tmp_path) -> None:
    def _raise(**kwargs):
        raise RuntimeError("model broken")

    monkeypatch.setattr(spawn_module, "_SubAgent", _raise)

    chunks = await _collect(_make_tool(tmp_path), task="x")

    assert chunks[-1].is_last is True
    assert "创建失败" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_subagent_execution_failure(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [RuntimeError("boom")]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)

    chunks = await _collect(_make_tool(tmp_path), task="x")

    assert chunks[-1].is_last is True
    assert "异常" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_poll_timeout_waits_for_delayed_event(monkeypatch, tmp_path) -> None:
    class DelayedSubAgent(FakeSubAgent):
        def reply_stream(self, inputs=None):
            async def gen():
                await asyncio.sleep(0.03)
                yield Msg(
                    name="sub",
                    role="assistant",
                    content=[TextBlock(type="text", text="delayed")],
                )

            return gen()

    monkeypatch.setattr(spawn_module, "_SubAgent", DelayedSubAgent)
    monkeypatch.setattr(spawn_module, "TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(spawn_module, "STREAM_POLL_SECONDS", 0.01)

    chunks = await _collect(_make_tool(tmp_path), task="x")

    assert chunks[-1].is_last is True
    assert "delayed" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_timeout(monkeypatch, tmp_path) -> None:
    class HangingSubAgent(FakeSubAgent):
        def reply_stream(self, inputs=None):
            async def gen():
                await asyncio.sleep(1)
                yield Msg(
                    name="sub",
                    role="assistant",
                    content=[TextBlock(type="text", text="late")],
                )

            return gen()

    monkeypatch.setattr(spawn_module, "_SubAgent", HangingSubAgent)
    monkeypatch.setattr(spawn_module, "TIMEOUT_SECONDS", 0.01)

    chunks = await _collect(_make_tool(tmp_path), task="x")

    assert chunks[-1].is_last is True
    assert "超时" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_streaming_yields_deltas(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [
        TextBlockDeltaEvent(reply_id="r", block_id="b", delta="Hello"),
        TextBlockDeltaEvent(reply_id="r", block_id="b", delta=" world"),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)

    chunks = await _collect(_make_tool(tmp_path), task="x")
    streaming = [chunk for chunk in chunks if not chunk.is_last]

    assert [_chunk_text(chunk) for chunk in streaming[:2]] == ["Hello", " world"]
    assert chunks[-1].metadata["subagent_trace"]["entries"][-1]["text"] == (
        "Hello world"
    )


@pytest.mark.asyncio
async def test_tool_call_and_result_trace(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [
        ToolCallStartEvent(
            reply_id="r",
            tool_call_id="tc1",
            tool_call_name="mcp__context-manager__execute_sql",
        ),
        ToolCallDeltaEvent(
            reply_id="r",
            tool_call_id="tc1",
            delta='{"query":"SELECT 1"}',
        ),
        ToolCallEndEvent(reply_id="r", tool_call_id="tc1"),
        ToolResultTextDeltaEvent(
            reply_id="r",
            tool_call_id="tc1",
            delta="rows: 42",
        ),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    rs = await _runtime_with_in_progress()

    chunks = await _collect(_make_tool(tmp_path, rs), task="x")

    trace = chunks[-1].metadata["subagent_trace"]
    assert trace["node_id"] == "n1"
    assert "tool_call" in [entry["type"] for entry in trace["entries"]]
    result_entry = next(
        entry for entry in trace["entries"] if entry["type"] == "tool_result"
    )
    assert result_entry["name"] == "mcp__context-manager__execute_sql"
    assert "rows: 42" in result_entry["output"]


@pytest.mark.asyncio
async def test_trace_metadata_without_active_node(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [
        TextBlockDeltaEvent(reply_id="r", block_id="b", delta="thinking"),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    rs = RuntimeStateManager()

    chunks = await _collect(_make_tool(tmp_path, rs), task="x")

    assert chunks[-1].metadata["subagent_trace"]["node_id"] is None
    prompt = FakeSubAgent.instances[0].kwargs["system_prompt"]
    assert "当前会话产物必须保存到 `artifacts/s1/`" in prompt
    assert "artifacts/s1/<graph_id>" not in prompt
    assert rs._traces == {}


@pytest.mark.asyncio
async def test_concurrent_calls_can_overlap(monkeypatch, tmp_path) -> None:
    active = 0
    max_active = 0

    class SlowSubAgent(FakeSubAgent):
        def reply_stream(self, inputs=None):
            async def gen():
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.05)
                active -= 1
                yield Msg(
                    name="sub",
                    role="assistant",
                    content=[TextBlock(type="text", text="done")],
                )

            return gen()

    monkeypatch.setattr(spawn_module, "_SubAgent", SlowSubAgent)
    tool = _make_tool(tmp_path)

    await asyncio.gather(
        _collect(tool, task="a"),
        _collect(tool, task="b"),
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_total_spawn_limit(monkeypatch, tmp_path) -> None:
    FakeSubAgent.events = [
        Msg(
            name="sub",
            role="assistant",
            content=[TextBlock(type="text", text="done")],
        ),
    ]
    FakeSubAgent.instances = []
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    monkeypatch.setattr(spawn_module, "MAX_TOTAL_SPAWNS", 2)
    tool = _make_tool(tmp_path)

    await _collect(tool, task="a")
    await _collect(tool, task="b")
    chunks = await _collect(tool, task="c")

    assert "spawn 上限" in _chunk_text(chunks[-1])


@pytest.mark.asyncio
async def test_subagent_injects_datasource_metadata(monkeypatch) -> None:
    async def _fake_execute(self, tool_call, kept_rules=None):
        yield "ok"

    monkeypatch.setattr(Agent, "_execute_tool_call", _fake_execute)
    sub_agent = _SubAgent.__new__(_SubAgent)
    sub_agent._request_context = {"datasource_id": "mysql-abc123"}
    sub_agent._cm_mcp_tool_prefixes = {"mcp__context-manager__"}
    tool_call = ToolCallBlock(
        id="call-1",
        name="mcp__context-manager__execute_sql",
        input='{"query":"select 1"}',
    )

    async for _ in _SubAgent._execute_tool_call(sub_agent, tool_call):
        pass

    assert json.loads(tool_call.input) == {
        "query": "select 1",
        "metadata": {"datasource_id": "mysql-abc123"},
    }
