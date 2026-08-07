from __future__ import annotations

import json
import logging

from agentscope.message import AssistantMsg, ToolCallBlock, ToolCallState
from agentscope.state import AgentState

from datapaw.host.core.agent.tool_diagnostics import log_awaiting_tool_calls


def _tool_call(name: str, input_text: str = "{}") -> ToolCallBlock:
    return ToolCallBlock(id="call-1", name=name, input=input_text)


def test_logs_asking_tool_call_details(caplog) -> None:
    state = AgentState()
    tool_call = _tool_call(
        "Bash",
        json.dumps(
            {
                "command": "python build_report.py --sections sections.json",
                "description": "Build report",
            },
        ),
    )
    tool_call.state = ToolCallState.ASKING
    state.context.append(
        AssistantMsg(
            name="default",
            content=[tool_call],
        ),
    )

    caplog.set_level(
        logging.ERROR,
        logger="datapaw.host.core.agent.tool_diagnostics",
    )

    log_awaiting_tool_calls(
        agent_name="default",
        state=state,
        source="test",
    )

    assert "pending_tools=" in caplog.text
    assert "Bash" in caplog.text
    assert "state': 'asking'" in caplog.text
    assert "command=python build_report.py --sections sections.json" in caplog.text


def test_logs_submitted_tool_call_without_result(caplog) -> None:
    state = AgentState()
    tool_call = _tool_call(
        "mcp__context-manager__execute_sql",
        json.dumps({"query": "select count(*) from access_logs"}),
    )
    tool_call.state = ToolCallState.SUBMITTED
    state.context.append(
        AssistantMsg(
            name="default",
            content=[tool_call],
        ),
    )

    caplog.set_level(
        logging.ERROR,
        logger="datapaw.host.core.agent.tool_diagnostics",
    )

    log_awaiting_tool_calls(
        agent_name="default",
        state=state,
        source="test",
    )

    assert "pending_tools=" in caplog.text
    assert "mcp__context-manager__execute_sql" in caplog.text
    assert "state': 'submitted'" in caplog.text
    assert "query=select count(*) from access_logs" in caplog.text
