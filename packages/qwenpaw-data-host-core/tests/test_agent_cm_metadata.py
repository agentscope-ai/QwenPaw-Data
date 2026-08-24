from __future__ import annotations

import json

from agentscope.message import ToolCallBlock

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent


def _make_agent(
    *,
    datasource_id: str | None = "mysql-abc123",
    prefixes: set[str] | None = None,
) -> QwenPawDataAgent:
    agent = QwenPawDataAgent.__new__(QwenPawDataAgent)
    agent._request_context = (
        {"datasource_id": datasource_id}
        if datasource_id is not None
        else {}
    )
    agent._cm_mcp_tool_prefixes = prefixes or {"mcp__context-manager__"}
    return agent


def _tool_call(name: str, input_text: str = "{}") -> ToolCallBlock:
    return ToolCallBlock(id="call-1", name=name, input=input_text)


def test_inject_replaces_metadata_for_cm_tool() -> None:
    agent = _make_agent()
    tool_call = _tool_call(
        "mcp__context-manager__execute_sql",
        json.dumps(
            {
                "query": "select 1",
                "metadata": {"datasource_id": "llm-wrong", "keep": "drop"},
            },
        ),
    )

    agent._inject_datasource_metadata(tool_call)

    payload = json.loads(tool_call.input)
    assert payload["metadata"] == {"datasource_id": "mysql-abc123"}
    assert payload["query"] == "select 1"


def test_inject_skips_non_cm_tool() -> None:
    agent = _make_agent()
    tool_call = _tool_call("mcp__other__execute_sql", '{"query": "select 1"}')

    agent._inject_datasource_metadata(tool_call)

    assert json.loads(tool_call.input) == {"query": "select 1"}


def test_inject_skips_when_no_datasource_id() -> None:
    agent = _make_agent(datasource_id=None)
    tool_call = _tool_call(
        "mcp__context-manager__execute_sql",
        '{"query": "select 1"}',
    )

    agent._inject_datasource_metadata(tool_call)

    assert json.loads(tool_call.input) == {"query": "select 1"}


def test_inject_creates_input_when_missing() -> None:
    agent = _make_agent()
    tool_call = _tool_call("mcp__context-manager__execute_sql", "")

    agent._inject_datasource_metadata(tool_call)

    assert json.loads(tool_call.input) == {
        "metadata": {"datasource_id": "mysql-abc123"},
    }


def test_inject_handles_malformed_input() -> None:
    agent = _make_agent()
    tool_call = _tool_call("mcp__context-manager__execute_sql", "not json")

    agent._inject_datasource_metadata(tool_call)

    assert json.loads(tool_call.input) == {
        "metadata": {"datasource_id": "mysql-abc123"},
    }
