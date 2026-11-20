"""Service task state replaces legacy standalone graph reply metadata."""

from __future__ import annotations

import json

import pytest
from agentscope.agent._config import ReActConfig
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.state import AgentState, TaskContext
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.tools.plan_tools import build_plan_tools
from qwenpaw_data.host.core.utils.msg import user_msg


class _Formatter:
    supported_input_media_types: list[str] = []


class ScriptedModel(ChatModelBase):
    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        self.model = "service-plan-test"
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
            content = [
                ToolCallBlock(
                    id=f"call-{self.sequence}",
                    name=name,
                    input=json.dumps(payload),
                )
            ]
        else:
            content = [TextBlock(text="Analysis reply.")]
        return ChatResponse(
            content=content,
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def _create_plan(subject: str = "Analyze") -> tuple[str, dict]:
    return "PlanCreate", {
        "tasks": [
            {"id": "analyze", "subject": subject, "description": "Analyze data."}
        ],
    }


def _completion_calls() -> list[tuple[str, dict]]:
    return [
        _create_plan(),
        ("TaskStateUpdate", {"task_id": "analyze", "state": "in_progress"}),
        ("TaskStateUpdate", {"task_id": "analyze", "state": "completed"}),
    ]


def _agent(calls: list[tuple[str, dict]], **kwargs) -> QwenPawDataAgent:
    return QwenPawDataAgent(
        name="service-plan-test",
        system_prompt="",
        model=ScriptedModel(calls),
        toolkit=Toolkit(tools=build_plan_tools()),
        react_config=ReActConfig(max_iters=10, interruption_raise_cancelled_error=True),
        **kwargs,
    )


def _assert_no_standalone_metadata(msg) -> None:
    assert not {"graph_id", "completed", "nodes", "artifacts"} & msg.metadata.keys()


async def test_completed_tasks_remain_in_serializable_service_state() -> None:
    agent = _agent(_completion_calls())
    msg = await agent.reply(user_msg("Run the analysis."))

    assert isinstance(agent.get_plan(), TaskContext)
    assert agent.get_plan() is agent.state.tasks_context
    assert [(t.id, t.state) for t in agent.get_plan().tasks] == [
        ("analyze", "completed"),
    ]
    restored = AgentState.model_validate(agent.state.model_dump(mode="json"))
    assert restored.tasks_context == agent.get_plan()
    _assert_no_standalone_metadata(msg)


async def test_followup_does_not_synthesize_a_new_completion_claim() -> None:
    agent = _agent(_completion_calls())
    await agent.reply(user_msg("Run the analysis."))
    before = agent.get_plan().model_dump(mode="json")

    second = await agent.reply(user_msg("Hello."))

    assert agent.get_plan().model_dump(mode="json") == before
    _assert_no_standalone_metadata(second)


async def test_new_plan_replaces_completed_tasks_without_implicit_dag_loop() -> None:
    agent = _agent(_completion_calls() + [_create_plan("Follow-up")])
    msg = await agent.reply(user_msg("Finish one analysis and start another."))

    assert [(t.subject, t.state) for t in agent.get_plan().tasks] == [
        ("Follow-up", "pending"),
    ]
    # Four tool turns plus one answer, no hidden standalone continuation turns.
    assert agent.model.sequence == 5
    _assert_no_standalone_metadata(msg)


@pytest.mark.parametrize(
    "option",
    [
        "runtime_state",
        "mode",
        "session_id",
        "workspace_dir",
        "artifact_dir",
        "session_trace_writer",
        "confirmation_handler",
    ],
)
def test_standalone_options_are_explicitly_unsupported(option) -> None:
    with pytest.raises(TypeError, match=option):
        _agent([], **{option: object()})


def test_agent_exposes_no_standalone_dag_api() -> None:
    agent = _agent([])
    for name in (
        "plan_notebook",
        "execute_sop",
        "set_mode",
        "get_plan_states",
        "set_execution_metadata",
        "_dag",
    ):
        assert not hasattr(agent, name)
