from __future__ import annotations

import json
import logging

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit, ToolChunk, ToolGroup, ToolResponse

from datapaw.host.core.agent.datapaw_agent import DataPawAgent
from datapaw.host.core.orchestration import RuntimeStateManager
from datapaw.host.core.orchestration.middleware import DataPawTraceMiddleware
from datapaw.host.core.orchestration.tools import build_datapaw_tools
from datapaw.host.core.session import JSONSessionStore
from datapaw.host.core.utils.ids import create_session_id
from datapaw.host.core.utils.msg import user_msg


class FakeEvent:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_dict(self) -> dict:
        return dict(self.payload)


async def _yield_events(events: list[FakeEvent]):
    for event in events:
        yield event


async def _yield_items(items: list):
    for item in items:
        yield item


class _FakeFormatter:
    # AgentScope 2.0.6 consults the model formatter for media-type support
    # during agent input validation.
    supported_input_media_types: list[str] = []


class FakePlanModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "fake-plan-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.calls = 0
        self.formatter = _FakeFormatter()

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice=None,
        **kwargs,
    ) -> ChatResponse:
        _ = (model_name, messages, tools, tool_choice, kwargs)
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="call-create",
                        name="create_plan",
                        input=json.dumps(
                            {
                                "name": "Trace plan",
                                "description": "Collect trace",
                                "expected_outcome": "Trace includes tools",
                                "nodes": [
                                    {
                                        "node_id": "fetch_data",
                                        "name": "Fetch data",
                                        "description": "Fetch data",
                                        "expected_outcome": "Dataset",
                                    },
                                ],
                            },
                        ),
                    ),
                ],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )
        return ChatResponse(
            content=[TextBlock(text="计划已创建。")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


@pytest.mark.asyncio
async def test_json_session_store_appends_trace_events(tmp_path) -> None:
    store = JSONSessionStore(tmp_path / "sessions" / "console")
    assert (
        store.get_path("session-a")
        == tmp_path / "sessions" / "console" / "default_session-a.json"
    )

    await store.update_session_state(
        "session-a",
        "agent.mode",
        "plan",
        create_if_not_exist=True,
    )

    await store.append_trace_event(
        "session-a",
        {
            "phase": "input",
            "graph_id": None,
            "node_id": None,
            "event": {
                "id": "msg-user",
                "name": "user",
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
                "metadata": {},
                "created_at": "2026-06-24T13:00:00.123456",
            },
        },
    )
    await store.append_trace_event(
        "session-a",
        {
            "phase": "acting",
            "graph_id": "graph-a",
            "node_id": None,
            "event": {
                "type": "ToolCallExecutionDeltaEvent",
                "tool_call_id": "call-create",
                "tool_call_name": "create_plan",
                "delta": '{"name":"Trace plan"}',
            },
        },
    )
    await store.append_trace_event(
        "session-a",
        {
            "phase": "acting",
            "graph_id": "graph-a",
            "node_id": None,
            "event": {
                "type": "ToolResultExecutionEndEvent",
                "tool_call_id": "call-create",
                "tool_call_name": "create_plan",
                "state": "success",
                "response": {
                    "content": [{"type": "text", "text": "created"}],
                    "state": "success",
                },
            },
        },
    )

    state = await store.get_session_state_dict("session-a")
    assert state["agent"]["mode"] == "plan"
    memory = state["agent"]["memory"]["content"]
    assert memory[0] == [
        {
            "id": "msg-user",
            "name": "user",
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
            "metadata": {
                "graph_id": None,
                "node_id": None,
            },
            "timestamp": "2026-06-24 13:00:00.123",
        },
        [],
    ]
    assert memory[1][0]["content"] == [
        {
            "type": "tool_use",
            "id": "call-create",
            "name": "create_plan",
            "input": {"name": "Trace plan"},
            "raw_input": '{"name":"Trace plan"}',
        },
    ]
    assert memory[1][0]["metadata"] == {
        "graph_id": "graph-a",
        "node_id": None,
    }
    assert memory[2][0]["role"] == "system"
    assert memory[2][0]["content"] == [
        {
            "type": "tool_result",
            "id": "call-create",
            "name": "create_plan",
            "output": [{"type": "text", "text": "created"}],
        },
    ]
    assert memory[2][0]["metadata"] == {
        "graph_id": "graph-a",
        "node_id": None,
    }


@pytest.mark.asyncio
async def test_json_session_store_preserves_subagent_trace_metadata(tmp_path) -> None:
    store = JSONSessionStore(tmp_path / "sessions" / "console")
    subagent_trace = {
        "type": "subagent_trace",
        "agent_name": "subagent-data_fetcher-n1-abcd",
        "entries": [{"type": "thinking", "text": "done"}],
    }

    await store.append_trace_event(
        "session-a",
        {
            "phase": "acting",
            "graph_id": "graph-a",
            "node_id": "n1",
            "event": {
                "type": "ToolResultExecutionEndEvent",
                "tool_call_id": "call-sub",
                "tool_call_name": "spawn_subagent",
                "state": "success",
                "response": {
                    "content": [{"type": "text", "text": "sub done"}],
                    "state": "success",
                    "metadata": {
                        "subagent_event": "summary",
                        "subagent_trace": subagent_trace,
                    },
                },
            },
        },
    )

    state = await store.get_session_state_dict("session-a")
    message = state["agent"]["memory"]["content"][0][0]
    assert message["metadata"] == {
        "subagent_event": "summary",
        "subagent_trace": subagent_trace,
        "graph_id": "graph-a",
        "node_id": "n1",
    }


def test_create_session_id_uses_millisecond_timestamp_and_short_uuid() -> None:
    session_id = create_session_id()
    timestamp, uuid_value = session_id.split("_", 1)

    assert timestamp.isdigit()
    assert len(timestamp) >= 13
    assert len(uuid_value) == 4
    int(uuid_value, 16)


@pytest.mark.asyncio
async def test_agent_session_trace_records_input_and_executed_create_plan() -> None:
    rs = RuntimeStateManager()
    session_traces: list[dict] = []

    async def write_session_trace(entry: dict) -> None:
        session_traces.append(entry)

    toolkit = Toolkit(
        tool_groups=[
            ToolGroup(
                name="plan",
                description="Planning tools.",
                tools=build_datapaw_tools(rs, mode="plan"),
            ),
        ],
    )
    agent = DataPawAgent(
        name="default",
        system_prompt="",
        model=FakePlanModel(),
        toolkit=toolkit,
        runtime_state=rs,
        mode="plan",
        session_trace_writer=write_session_trace,
    )

    await agent.reply(user_msg("分析 WebApp 12月访问趋势"))

    assert any(
        entry["phase"] == "input"
        and entry["event"]["role"] == "user"
        and entry["event"]["content"][0]["text"] == "分析 WebApp 12月访问趋势"
        for entry in session_traces
    )
    assert any(
        entry["phase"] == "acting"
        and entry["event"].get("type") == "ToolCallExecutionStartEvent"
        and entry["event"].get("tool_call_name") == "create_plan"
        for entry in session_traces
    )
    assert any(
        entry["phase"] == "acting"
        and entry["event"].get("type") == "ToolResultExecutionEndEvent"
        and entry["event"].get("tool_call_name") == "create_plan"
        and entry["event"].get("state") == ToolResultState.SUCCESS
        for entry in session_traces
    )
    assert rs.current_graph_id is not None


@pytest.mark.asyncio
async def test_trace_middleware_records_actual_mcp_tool_execution() -> None:
    rs = RuntimeStateManager()
    session_traces: list[dict] = []

    async def write_session_trace(entry: dict) -> None:
        session_traces.append(entry)

    middleware = DataPawTraceMiddleware(
        rs,
        session_trace_writer=write_session_trace,
    )
    tool_call = ToolCallBlock(
        id="call-mcp",
        name="mcp__context-manager__execute_sql",
        input='{"query":"select 1"}',
    )
    items = [
        ToolChunk(content=[TextBlock(text="rows preview")]),
        ToolResponse(
            id="call-mcp",
            content=[TextBlock(text="rows preview")],
            state=ToolResultState.SUCCESS,
        ),
    ]

    async for _ in middleware.on_acting(
        None,
        {"tool_call": tool_call},
        lambda: _yield_items(items),
    ):
        pass

    assert any(
        entry["event"].get("type") == "ToolCallExecutionStartEvent"
        and entry["event"].get("tool_call_name")
        == "mcp__context-manager__execute_sql"
        for entry in session_traces
    )
    assert any(
        entry["event"].get("type") == "ToolCallExecutionDeltaEvent"
        and entry["event"].get("tool_call_name")
        == "mcp__context-manager__execute_sql"
        for entry in session_traces
    )
    assert any(
        entry["event"].get("type") == "ToolResultExecutionDeltaEvent"
        and entry["event"].get("tool_call_name")
        == "mcp__context-manager__execute_sql"
        and entry["event"].get("delta") == "rows preview"
        for entry in session_traces
    )
    assert any(
        entry["event"].get("type") == "ToolResultExecutionEndEvent"
        and entry["event"].get("tool_call_name")
        == "mcp__context-manager__execute_sql"
        and entry["event"].get("state") == ToolResultState.SUCCESS
        for entry in session_traces
    )


@pytest.mark.asyncio
async def test_trace_middleware_writes_session_superset_of_node_trace(caplog) -> None:
    rs = RuntimeStateManager()
    session_traces: list[dict] = []
    caplog.set_level(logging.INFO, logger="datapaw.host.core.orchestration.middleware")

    async def write_session_trace(entry: dict) -> None:
        session_traces.append(entry)

    middleware = DataPawTraceMiddleware(
        rs,
        session_trace_writer=write_session_trace,
    )
    planning_events = [
        FakeEvent(
            {
                "type": "ThinkingBlockStartEvent",
                "reply_id": "reply-1",
                "block_id": "think-1",
            },
        ),
        FakeEvent(
            {
                "type": "ThinkingBlockDeltaEvent",
                "reply_id": "reply-1",
                "block_id": "think-1",
                "delta": "planning ",
            },
        ),
        FakeEvent(
            {
                "type": "ThinkingBlockDeltaEvent",
                "reply_id": "reply-1",
                "block_id": "think-1",
                "delta": "complete",
            },
        ),
        FakeEvent(
            {
                "type": "ThinkingBlockEndEvent",
                "reply_id": "reply-1",
                "block_id": "think-1",
            },
        ),
    ]
    async for _ in middleware.on_reasoning(
        None,
        {},
        lambda: _yield_events(planning_events),
    ):
        pass

    assert [entry["event"] for entry in session_traces] == [
        event.to_dict() for event in planning_events
    ]
    assert rs.state_dict()["traces"] == {}
    assert (
        "DataPaw trace phase=reasoning graph_id=- node_id=- "
        "thinking=planning complete"
    ) in caplog.text
    assert "ThinkingBlockDeltaEvent" not in caplog.text

    await rs.create_plan(
        name="Trace plan",
        description="Collect node trace",
        expected_outcome="Trace is persisted",
        nodes=[
            {
                "node_id": "fetch_data",
                "name": "Fetch data",
                "description": "Fetch data",
                "expected_outcome": "Dataset",
            },
        ],
    )
    await rs.update_subtask("fetch_data", "in_progress")
    graph_id = rs.current_graph_id
    tool_events = [
        FakeEvent(
            {
                "type": "ToolCallStartEvent",
                "tool_call_id": "call-1",
                "tool_call_name": "Bash",
            },
        ),
        FakeEvent(
            {
                "type": "ToolCallDeltaEvent",
                "tool_call_id": "call-1",
                "delta": "{\"cmd\":\"echo ",
            },
        ),
        FakeEvent(
            {
                "type": "ToolCallDeltaEvent",
                "tool_call_id": "call-1",
                "delta": "hello\"}",
            },
        ),
        FakeEvent(
            {
                "type": "ToolCallEndEvent",
                "tool_call_id": "call-1",
            },
        ),
        FakeEvent(
            {
                "type": "ToolResultStartEvent",
                "tool_call_id": "call-1",
                "tool_call_name": "Bash",
            },
        ),
        FakeEvent(
            {
                "type": "ToolResultTextDeltaEvent",
                "tool_call_id": "call-1",
                "delta": "hello\n",
            },
        ),
        FakeEvent(
            {
                "type": "ToolResultTextDeltaEvent",
                "tool_call_id": "call-1",
                "delta": "done",
            },
        ),
        FakeEvent(
            {
                "type": "ToolResultEndEvent",
                "tool_call_id": "call-1",
                "state": "success",
            },
        ),
    ]

    async for _ in middleware.on_acting(None, {}, lambda: _yield_events(tool_events)):
        pass

    assert session_traces[-1] == {
        "phase": "acting",
        "graph_id": graph_id,
        "node_id": "fetch_data",
        "event": tool_events[-1].to_dict(),
    }
    assert rs.state_dict()["traces"] == {
        "fetch_data": [event.to_dict() for event in tool_events],
    }
    assert (
        f"DataPaw trace phase=acting graph_id={graph_id} "
        "node_id=fetch_data tool_call tool=Bash tool_call_id=call-1 "
        "input={\"cmd\":\"echo hello\"}"
    ) in caplog.text
    assert (
        f"DataPaw trace phase=acting graph_id={graph_id} "
        "node_id=fetch_data tool_result tool=Bash tool_call_id=call-1 "
        "state=success result=hello\\ndone"
    ) in caplog.text
    assert "ToolCallDeltaEvent" not in caplog.text
    assert "ToolResultTextDeltaEvent" not in caplog.text
