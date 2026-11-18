from __future__ import annotations

import asyncio
import json
from pathlib import Path, PureWindowsPath
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
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.skill import Skill
from agentscope.state import AgentState, Task, TaskContext
from agentscope.tool import Toolkit, ToolResponse

import qwenpaw_data.host.core.agent.spawn_subagent as spawn_module
from qwenpaw_data.host.core.agent.middleware.sql_artifact import SqlArtifactMiddleware
from qwenpaw_data.host.core.agent.middleware.tool_timeout import ToolTimeoutMiddleware
from qwenpaw_data.host.core.agent.spawn_subagent import (
    SpawnSubagent,
    _SubAgent,
    _build_sub_prompt,
)
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.agent.subagent_result import SubmitResult
from qwenpaw_data.host.core.paths import UserAgentPaths
from .conftest import TEST_IDENTITY


def test_subagent_prompt_preserves_linux_sandbox_paths_on_windows():
    prompt = spawn_module._build_environment_section(
        workspace_dir=PureWindowsPath("/workspace"),
        artifacts_root=PureWindowsPath("/workspace/artifacts"),
        session_id="session",
    )
    assert "`/workspace`" in prompt
    assert "`/workspace/artifacts/session`" in prompt
    assert "\\" not in prompt


def _chunk_text(chunk) -> str:
    return "".join(getattr(block, "text", "") for block in chunk.content)


async def _collect(tool: SpawnSubagent, **kwargs):
    return [chunk async for chunk in tool(**kwargs)]


async def _runtime_with_in_progress() -> TaskContext:
    return TaskContext(
        tasks=[
            Task(
                id="n0",
                subject="upstream",
                description="upstream summary",
                metadata={},
                state="completed",
            ),
            Task(
                id="n1",
                subject="current",
                description="d",
                metadata={},
                state="in_progress",
                blocked_by=["n0"],
            ),
        ]
    )


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
    rs: TaskContext | None = None,
    *,
    request_context: dict | None = None,
) -> SpawnSubagent:
    workspace = SimpleNamespace(workdir=str(tmp_path / "workspace"))
    paths = UserAgentPaths.from_identity(
        home=tmp_path, identity=TEST_IDENTITY, agent_id="test-agent", session_id="s1"
    )
    run_context = RunContext(
        session_id="s1",
        chat_id="c1",
        workspace=workspace,
        paths=paths,
        identity=TEST_IDENTITY,
        session_factory=SimpleNamespace(),
        request_context=request_context or {},
        user_runtime_config=SimpleNamespace(
            tool_timeout_enabled=True,
            tool_timeout_seconds=30,
            tool_confirmation_level="off",
            react_max_iters=19,
        ),
    )
    return SpawnSubagent(
        run_context=run_context,
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
                tasks_context=rs or TaskContext(),
                permission_context=PermissionContext(
                    mode=PermissionMode.ACCEPT_EDITS,
                ),
            ),
        ),
        cm_mcp_tool_prefixes={"mcp__context-manager__"},
    )


def _role_dir(tmp_path):
    paths = UserAgentPaths.from_identity(
        home=tmp_path,
        identity=TEST_IDENTITY,
        agent_id="test-agent",
        session_id="s1",
    )
    directory = paths.workspace / "agents" / paths.agent_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def test_handoff_is_single_use_and_requires_session_relative_artifact_names():
    submit = SubmitResult()
    with pytest.raises(ValueError, match="session-relative"):
        await submit.call(summary="ok", artifacts=[{"path": "/report.html"}])
    assert submit.delivery is None
    await submit.call(summary="ok", artifacts=[{"path": "daily/report.html"}])
    assert submit.delivery.artifacts[0].path == "daily/report.html"
    with pytest.raises(ValueError, match="already submitted"):
        await submit.call(summary="another")


def test_workspace_roles_are_per_agent_and_cannot_override_presets(tmp_path):
    directory = _role_dir(tmp_path)
    isolated = directory.parent / "other"
    isolated.mkdir()
    (isolated / "not-mine.md").write_text("---\ndescription: Other agent\n---\n")
    (directory / "local.md").write_text(
        "---\ndescription: Local role\ntools: []\n---\n"
    )
    tool = _make_tool(tmp_path)
    assert set(tool.input_schema["properties"]["type"]["enum"]) == {
        "general",
        "report",
        "data_fetcher",
        "local",
    }
    assert "local: Local role" in tool.description
    (directory / "duplicate.md").write_text(
        "---\nname: report\ndescription: Other\n---\n"
    )
    with pytest.raises(ValueError, match="duplicate subagent role: report"):
        _make_tool(tmp_path)


@pytest.mark.parametrize("selector", ["tools: Read", "mcp: null", "tool: []"])
def test_workspace_roles_reject_invalid_selectors(tmp_path, selector):
    (_role_dir(tmp_path) / "bad.md").write_text(
        f"---\ndescription: Bad\n{selector}\n---\n"
    )
    with pytest.raises(ValueError, match="invalid subagent role"):
        _make_tool(tmp_path)


@pytest.mark.parametrize("role", ["general", "report", "scoped"])
async def test_role_toolkit_and_missing_handoff(monkeypatch, tmp_path, role):
    (_role_dir(tmp_path) / "scoped.md").write_text(
        "---\ndescription: Scoped\ntools: [Read]\nskills: []\nmcp: [context-manager]\n---\n"
        "Scoped test instructions."
    )
    tool = _make_tool(tmp_path)
    tool._workspace_tools.extend(FakeWorkspaceTool(n) for n in ("Edit", "Glob", "Grep"))
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    monkeypatch.setattr(FakeSubAgent, "events", [])
    monkeypatch.setattr(FakeSubAgent, "instances", [])
    chunks = await _collect(
        tool, type=role, description="Scoped task", prompt="Summarize"
    )
    assert chunks[-1].state == ToolResultState.ERROR
    assert json.loads(_chunk_text(chunks[-1]))["status"] == "error"
    kwargs = FakeSubAgent.instances[0].kwargs
    assert kwargs["state"].permission_context.mode == PermissionMode.ACCEPT_EDITS
    assert isinstance(
        kwargs["middlewares"][0], spawn_module._UnattendedToolConfirmation
    )
    toolkit = kwargs["toolkit"]
    names = {s["function"]["name"] for s in await toolkit.get_tool_schemas()}
    assert {"submit_result", "Read"} <= names
    assert "spawn_subagent" not in names and "ask_user_question" not in names
    assert ("download_file" in names) == (role == "general")
    assert ("mcp__context-manager__execute_sql" in names) == (role != "report")
    skills = {s.name for group in toolkit.tool_groups for s in group.skills_or_loaders}
    assert (
        skills
        == {
            "general": {"fetch-data", "bi-report-generation"},
            "report": {"bi-report-generation"},
            "scoped": set(),
        }[role]
    )


async def test_requested_missing_capability_does_not_fall_back(tmp_path):
    (_role_dir(tmp_path) / "scoped.md").write_text(
        "---\ndescription: Scoped\nskills: [not-enabled]\n---\nUse the skill."
    )
    chunks = await _collect(
        _make_tool(tmp_path), type="scoped", description="Check", prompt="Check"
    )
    assert chunks[-1].state == ToolResultState.ERROR
    assert "unavailable skills: not-enabled" in _chunk_text(chunks[-1])


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
    )

    assert "查询数据" in prompt
    assert "按天聚合" in prompt
    assert "上游摘要" in prompt
    assert "Bash" in prompt
    assert "execute_sql" in prompt
    assert str(tmp_path / "workspace") in prompt
    assert str(tmp_path / "workspace/artifacts/s1") in prompt
    assert "相对当前 session artifacts 根" in prompt
    assert "graph_1" not in prompt


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

    assert f"当前会话产物必须保存到 `{tmp_path / 'workspace/artifacts/s1'}`" in prompt
    assert "文件路径使用 `<filename>`" in prompt
    assert "s1/<filename>" not in prompt
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
    environment = analysis_environment_hint(
        session_id="s1",
        workspace_dir="/workspace",
        artifact_dir="/workspace/artifacts/s1",
    )
    assert "workspace 根目录：`/workspace`" in environment
    assert "当前 Session 产物目录：`/workspace/artifacts/s1`" in environment
    assert "skills/default/bi-report-generation/SKILL.md" in agent_prompt
    # The shared environment follows native TaskContext landing rules, not the
    # retired embedded DAG's FileRef or a hard-coded default-agent skill path.
    assert "当前 Session 产物目录" in environment
    assert "未获用户明确指示，禁止读取或改写其他 Session 的文件" in environment
    assert "data/raw/" in environment
    assert "result.yaml" in environment

    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", "0")
    assert "spawn_subagent(task" not in build_master_prompt(mode="agent")


def test_analysis_environment_uses_local_host_paths(tmp_path) -> None:
    from qwenpaw_data.host.core.prompts import analysis_environment_hint

    workspace_dir = (tmp_path / "workspace").resolve()
    artifact_dir = workspace_dir / "artifacts" / "s1"
    environment = analysis_environment_hint(
        session_id="s1",
        workspace_dir=workspace_dir,
        artifact_dir=artifact_dir,
    )

    assert f"workspace 根目录：`{workspace_dir}`" in environment
    assert f"当前 Session 产物目录：`{artifact_dir}`" in environment
    assert "FileRef" not in environment
    assert "skills/default/" not in environment


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
    assert str(tmp_path / "workspace/artifacts/s1") in prompt
    assert "graph_id" not in prompt
    assert rs.tasks[1].state == "in_progress"  # Delegation does not advance the plan.


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
    kwargs = FakeSubAgent.instances[0].kwargs
    _, timeout_middleware, sql_middleware, _ = kwargs["middlewares"]
    assert isinstance(timeout_middleware, ToolTimeoutMiddleware)
    assert isinstance(sql_middleware, SqlArtifactMiddleware)
    assert sql_middleware._run_context.session_id == "s1"
    assert sql_middleware._run_context.paths.agent_id == "test-agent"
    assert (
        Path(
            sql_middleware._run_context.paths.agent_artifact_dir(
                sql_middleware._run_context.workspace
            )
        )
        == tmp_path / "workspace/artifacts/s1"
    )
    assert kwargs["react_config"].interruption_raise_cancelled_error is True
    assert kwargs["react_config"].max_iters == 19


@pytest.mark.asyncio
@pytest.mark.parametrize("workdir", ["local", "/workspace"])
async def test_spawned_agent_times_out_its_tools(monkeypatch, tmp_path, workdir):
    stopped = asyncio.Event()
    responses = []

    async def slow_tool(self, tool_call, state):
        try:
            yield ToolResponse(content=[TextBlock(text="partial")])
            await asyncio.Event().wait()
        finally:
            stopped.set()

    # Exercise the spawned Agent's actual acting middleware chain without an LLM.
    async def reply_stream(self, inputs=None):
        responses.extend(
            [
                item
                async for item in self._acting(
                    ToolCallBlock(id="slow-tool", name="Bash", input="{}"),
                )
            ]
        )
        yield Msg(name="sub", role="assistant", content="handled tool result")

    monkeypatch.setattr(Toolkit, "call_tool", slow_tool)
    monkeypatch.setattr(_SubAgent, "reply_stream", reply_stream)
    tool = _make_tool(tmp_path)
    tool._ctx.user_runtime_config.tool_timeout_seconds = 0.05
    if workdir != "local":
        tool._ctx.workspace.workdir = workdir

    await asyncio.wait_for(_collect(tool, task="query"), 2)

    assert stopped.is_set()
    assert len(responses) == 1
    assert responses[0].state == ToolResultState.ERROR
    error = json.loads(_chunk_text(responses[0]))["error"]
    assert error["code"] == "TOOL_TIMEOUT"
    assert error["tool"] == "Bash"
    assert error["timeout_seconds"] == 0.05


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
@pytest.mark.parametrize("cause", ["cancel", "tool_timeout", "child_timeout"])
@pytest.mark.parametrize("native_role", [False, True])
async def test_interrupted_spawn_drains_child_before_return(
    monkeypatch, tmp_path, cause, native_role
):
    started, stopped = asyncio.Event(), asyncio.Event()
    child_tasks = []

    class HangingSubAgent(FakeSubAgent):
        async def reply_stream(self, inputs=None):
            child_tasks.append(asyncio.current_task())
            try:
                started.set()
                await asyncio.Event().wait()
                yield TextBlockDeltaEvent(
                    reply_id="r", block_id="b", delta="late success"
                )
            finally:
                stopped.set()

    monkeypatch.setattr(spawn_module, "_SubAgent", HangingSubAgent)
    if cause == "child_timeout":
        monkeypatch.setattr(spawn_module, "TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT", 1)
    tool = _make_tool(tmp_path)

    invocation = (
        dict(type="general", description="Read", prompt="query")
        if native_role
        else dict(task="query")
    )

    async def call():
        if cause != "tool_timeout":
            return await _collect(tool, **invocation)

        async def handler(**kwargs):
            async for chunk in tool(**invocation):
                yield chunk

        return [
            chunk
            async for chunk in ToolTimeoutMiddleware(
                run_context=SimpleNamespace(
                    user_runtime_config=SimpleNamespace(
                        tool_timeout_enabled=True, tool_timeout_seconds=0.1
                    )
                )
            ).on_acting(
                None, {"tool_call": SimpleNamespace(name="spawn_subagent")}, handler
            )
        ]

    pending = asyncio.create_task(call())
    try:
        await asyncio.wait_for(started.wait(), 2)
        if cause == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            chunks = await asyncio.wait_for(pending, 2)
            assert chunks[-1].state == ToolResultState.ERROR
            assert all("late success" not in _chunk_text(c) for c in chunks)
        assert stopped.is_set(), "child generator was left running"
        assert all(t.done() for t in child_tasks), "child event task was orphaned"

        # The same tool remains usable; cancellation must release its slot.
        FakeSubAgent.events = [
            TextBlockDeltaEvent(reply_id="next", block_id="b", delta="done")
        ]
        monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
        resumed = await asyncio.wait_for(_collect(tool, task="follow up"), 2)
        assert resumed[-1].metadata["subagent_event"] == "summary"
        assert _chunk_text(resumed[-1]) == "done"
    finally:
        # Also clean up deliberately failing pre-fix runs.
        for task in [pending, *child_tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, *child_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_closing_spawn_stream_closes_nested_child(monkeypatch, tmp_path):
    stopped = asyncio.Event()
    streams = []

    class StreamingSubAgent(FakeSubAgent):
        def reply_stream(self, inputs=None):
            async def events():
                try:
                    yield TextBlockDeltaEvent(
                        reply_id="r", block_id="b", delta="partial"
                    )
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            stream = events()
            streams.append(stream)
            return stream

    monkeypatch.setattr(spawn_module, "_SubAgent", StreamingSubAgent)
    stream = _make_tool(tmp_path)(task="query")
    try:
        assert _chunk_text(await anext(stream)) == "partial"
        await stream.aclose()
        assert stopped.is_set(), "closing the outer stream must close the child stream"
    finally:
        await stream.aclose()
        for child in streams:
            await child.aclose()


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
    rs = TaskContext()

    chunks = await _collect(_make_tool(tmp_path, rs), task="x")

    assert chunks[-1].metadata["subagent_trace"]["node_id"] is None
    prompt = FakeSubAgent.instances[0].kwargs["system_prompt"]
    assert f"当前会话产物必须保存到 `{tmp_path / 'workspace/artifacts/s1'}`" in prompt
    assert "<graph_id>" not in prompt
    assert "s1/<filename>" not in prompt
    assert rs.tasks == []


async def test_multiple_active_tasks_do_not_guess_or_mutate_the_plan(
    monkeypatch, tmp_path
):
    plan = await _runtime_with_in_progress()
    plan.tasks.append(
        Task(
            id="other",
            subject="other",
            description="d",
            metadata={},
            state="in_progress",
        )
    )
    before = plan.model_dump()
    FakeSubAgent.events = [
        TextBlockDeltaEvent(reply_id="r", block_id="b", delta="done")
    ]
    monkeypatch.setattr(spawn_module, "_SubAgent", FakeSubAgent)
    chunks = await _collect(_make_tool(tmp_path, plan), task="explicit task")
    assert chunks[-1].metadata["subagent_trace"]["node_id"] is None
    assert plan.model_dump() == before


@pytest.mark.parametrize(
    "level,blocked", [("off", False), ("auto", True), ("all", True)]
)
async def test_child_tools_cannot_bypass_parent_approval(
    monkeypatch, tmp_path, level, blocked
):
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_TOOL_CONFIRMATION_AUTO_ASK_NAMES", '["Bash"]')
    clear_settings()
    tool = _make_tool(tmp_path)
    tool._ctx.user_runtime_config.tool_confirmation_level = level
    called = []

    async def execute(**kwargs):
        called.append(True)
        yield ToolResponse(content=[TextBlock(text="executed")])

    middleware = spawn_module._UnattendedToolConfirmation(tool._ctx)
    results = [
        item
        async for item in middleware.on_acting(
            None,
            {"tool_call": ToolCallBlock(id="child", name="Bash", input="{}")},
            execute,
        )
    ]
    assert bool(called) is not blocked
    assert (results[0].state == ToolResultState.DENIED) is blocked
    if blocked:
        assert "requires approval" in _chunk_text(results[0])


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
