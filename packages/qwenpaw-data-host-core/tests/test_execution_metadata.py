from __future__ import annotations

import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit, ToolGroup

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.orchestration import RuntimeStateManager
from qwenpaw_data.host.core.orchestration.tools import build_qwenpaw_data_tools
from qwenpaw_data.host.core.utils.msg import user_msg


class _Formatter:
    supported_input_media_types: list[str] = []


class ScriptedModel(ChatModelBase):
    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        self.model = "execution-metadata-test"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = _Formatter()
        self.calls = list(calls)
        self.sequence = 0

    async def _call_api(self, *args, **kwargs) -> ChatResponse:
        self.sequence += 1
        if self.calls:
            name, payload = self.calls.pop(0)
            content = [ToolCallBlock(
                id=f"call-{self.sequence}", name=name, input=json.dumps(payload),
            )]
        else:
            content = [TextBlock(text="Analysis reply.")]
        return ChatResponse(
            content=content,
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def _create_plan(name: str = "Analysis") -> tuple[str, dict]:
    return "create_plan", {
        "name": name,
        "description": "Verify execution metadata.",
        "expected_outcome": "An analysis result.",
        "nodes": [{
            "node_id": "analyze", "name": "Analyze", "description": "Analyze data.",
            "expected_outcome": "An analysis result.",
        }],
    }


def _completion_calls() -> list[tuple[str, dict]]:
    return [
        _create_plan(),
        ("update_subtask", {"node_id": "analyze", "state": "in_progress"}),
        ("update_subtask", {
            "node_id": "analyze", "state": "done",
            "reasoning": "Analyzed data.", "summary": "Analysis completed.",
        }),
        ("finish_plan", {"state": "done", "outcome": "Analysis completed."}),
    ]


def _agent(calls: list[tuple[str, dict]]) -> QwenPawDataAgent:
    runtime = RuntimeStateManager()
    return QwenPawDataAgent(
        name="metadata-test", system_prompt="", model=ScriptedModel(calls),
        toolkit=Toolkit(tool_groups=[ToolGroup(
            name="agent", description="Analysis tools.",
            tools=build_qwenpaw_data_tools(runtime),
        )]),
        runtime_state=runtime,
    )


@pytest.mark.asyncio
async def test_finished_plan_retains_graph_and_completed_nodes_in_reply() -> None:
    agent = _agent(_completion_calls())

    msg = await agent.reply(user_msg("Run the analysis."))

    assert agent.plan_notebook.current_graph_id is None
    assert msg.metadata["graph_id"] == agent.plan_notebook.last_finished_graph_id
    assert msg.metadata["graph_id"] is not None
    assert msg.metadata["completed"] is True
    assert msg.metadata["nodes"] == [{
        "id": "analyze", "name": "Analyze", "state": "done",
    }]
    assert "last_finished_graph_id" not in agent.plan_notebook.state_dict()


@pytest.mark.asyncio
async def test_next_graph_free_reply_does_not_reuse_finished_graph() -> None:
    agent = _agent(_completion_calls())
    first = await agent.reply(user_msg("Run the analysis."))
    assert first.metadata["completed"] is True

    second = await agent.reply(user_msg("Hello."))

    assert second.metadata["graph_id"] is None
    assert second.metadata["completed"] is False
    assert second.metadata["nodes"] == []


@pytest.mark.asyncio
async def test_active_new_plan_takes_precedence_over_finished_plan() -> None:
    agent = _agent(_completion_calls() + [_create_plan("Follow-up")])

    msg = await agent.reply(user_msg("Finish one analysis and start another."))

    assert msg.metadata["graph_id"] == agent.plan_notebook.current_graph_id
    assert msg.metadata["graph_id"] != agent.plan_notebook.last_finished_graph_id
    assert msg.metadata["completed"] is False
    assert msg.metadata["nodes"][0]["state"] == "todo"


@pytest.mark.asyncio
async def test_execute_sop_reports_graph_after_finish_plan() -> None:
    agent = _agent(_completion_calls()[1:])

    msg = await agent.execute_sop({
        "name": "Loaded analysis",
        "description": "Execute an existing plan.",
        "expected_outcome": "Analysis result.",
        "nodes": [{
            "node_id": "analyze", "name": "Analyze", "description": "Analyze data.",
            "expected_outcome": "Analysis result.", "deps": [],
        }],
    })

    assert agent.plan_notebook.current_graph_id is None
    assert msg.metadata["graph_id"] is not None
    assert msg.metadata["completed"] is True
    assert msg.metadata["nodes"][0]["state"] == "done"
