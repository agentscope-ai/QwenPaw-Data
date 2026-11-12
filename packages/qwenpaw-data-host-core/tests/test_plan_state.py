# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from agentscope.agent._config import ReActConfig
from agentscope.message import TextBlock, ToolResultState
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState, TaskContext
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.middleware import PlanPromptMiddleware
from qwenpaw_data.host.core.agent.tools.plan_tools import (
    PlanCreate,
    PlanUpdate,
    TaskStateUpdate,
)
from qwenpaw_data.host.core.utils.plan import to_task_context


def _new_state() -> AgentState:
    return AgentState(
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )


def _plan_payload() -> dict:
    return {
        "tasks": [
            {
                "id": "1",
                "subject": "Fetch data",
                "description": "Load source data",
                "metadata": {},
                "state": "pending",
                "blocked_by": [],
                "blocks": ["2"],
            },
            {
                "id": "2",
                "subject": "Analyze",
                "description": "Analyze source data",
                "metadata": {},
                "state": "pending",
                "blocked_by": ["1"],
                "blocks": [],
            },
        ],
    }


def test_restore_ignores_removed_plan_header() -> None:
    state = _new_state()
    payload = state.model_dump(mode="json")
    payload["tasks_context"] = {
        "plan": {
            "name": "Legacy",
            "description": "Legacy plan",
        },
        "tasks": [],
    }

    restored = AgentState.model_validate(payload)

    assert isinstance(restored.tasks_context, TaskContext)
    assert restored.tasks_context.tasks == []
    assert "plan" not in restored.model_dump(mode="json")["tasks_context"]


def test_dependencies_are_rebuilt_without_cycle_validation() -> None:
    context = to_task_context(
        {
            "tasks": [
                {
                    "id": "a",
                    "subject": "A",
                    "description": "A",
                    "metadata": {},
                    "blocked_by": ["b", "b"],
                },
                {
                    "id": "b",
                    "subject": "B",
                    "description": "B",
                    "metadata": {},
                    "blocked_by": ["a"],
                },
            ],
        },
    )

    assert context.tasks[0].blocked_by == ["b"]
    assert context.tasks[0].blocks == ["b"]
    assert context.tasks[1].blocked_by == ["a"]
    assert context.tasks[1].blocks == ["a"]


@pytest.mark.asyncio
async def test_three_plan_tools_keep_plan_after_all_tasks_complete() -> None:
    state = _new_state()

    await PlanCreate().call(
        _agent_state=state,
        tasks=[
            {
                "id": "1",
                "subject": "One",
                "description": "Task one",
            },
        ],
    )
    await TaskStateUpdate().call(
        _agent_state=state,
        task_id="1",
        state="completed",
    )
    original_created_at = state.tasks_context.tasks[0].created_at
    state.tasks_context.tasks[0].metadata["kept"] = True

    result = await PlanUpdate().call(
        _agent_state=state,
        changes=[
            {
                "task_id": "1",
                "action": "revise",
                "task": {
                    "subject": "One revised",
                    "description": "Revised",
                    "blocked_by": [],
                },
            },
        ],
    )

    assert result.state != ToolResultState.ERROR
    assert state.tasks_context.tasks[0].state == "pending"
    assert state.tasks_context.tasks[0].created_at == original_created_at
    assert state.tasks_context.tasks[0].metadata == {"kept": True}

    await PlanCreate().call(
        _agent_state=state,
        tasks=[
            {
                "id": "1",
                "subject": "Replacement task",
                "description": "Starts from scratch",
            },
        ],
    )
    assert state.tasks_context.tasks[0].state == "pending"
    assert state.tasks_context.tasks[0].metadata == {}
    assert state.tasks_context.tasks[0].subject == "Replacement task"


@pytest.mark.asyncio
async def test_plan_update_delete_rebuilds_dependencies_and_requires_plan() -> None:
    state = _new_state()
    missing = await PlanUpdate().call(_agent_state=state, changes=[])
    assert missing.state == ToolResultState.ERROR

    state.tasks_context = PlanCreate.build_context(
        [
            {
                "id": "1",
                "subject": "One",
                "description": "One",
            },
            {
                "id": "2",
                "subject": "Two",
                "description": "Two",
                "blocked_by": ["1"],
            },
        ],
    )
    result = await PlanUpdate().call(
        _agent_state=state,
        changes=[{"task_id": "1", "action": "delete"}],
    )

    assert result.state != ToolResultState.ERROR
    assert [task.id for task in state.tasks_context.tasks] == ["2"]
    assert state.tasks_context.tasks[0].blocked_by == []
    assert state.tasks_context.tasks[0].blocks == []


@pytest.mark.asyncio
async def test_task_state_update_does_not_enforce_dependencies() -> None:
    state = _new_state()
    state.tasks_context = PlanCreate.build_context(
        [
            {
                "id": "1",
                "subject": "Blocker",
                "description": "Still pending",
            },
            {
                "id": "2",
                "subject": "Blocked",
                "description": "Can still complete",
                "blocked_by": ["1"],
            },
        ],
    )

    result = await TaskStateUpdate().call(
        _agent_state=state,
        task_id="2",
        state="completed",
    )

    assert result.state != ToolResultState.ERROR
    assert state.tasks_context.tasks[0].state == "pending"
    assert state.tasks_context.tasks[1].state == "completed"


def test_plan_hint_contains_task_summary_only() -> None:
    hint = PlanPromptMiddleware().format_plan_hint(
        PlanCreate.build_context(
            [
                {
                    "id": "1",
                    "subject": "Task",
                    "description": "Task detail",
                },
            ],
        ),
    )

    assert "Name:" not in hint
    assert "Description:" not in hint
    assert "Unfinished: 1" in hint
    assert "1 [pending] Task" in hint


@pytest.mark.asyncio
async def test_plan_update_is_atomic_on_invalid_dependency() -> None:
    state = _new_state()
    state.tasks_context = PlanCreate.build_context(
        [
            {
                "id": "1",
                "subject": "One",
                "description": "One",
            },
        ],
    )
    before = state.tasks_context.model_dump(mode="json")

    response = await PlanUpdate().call(
        _agent_state=state,
        changes=[
            {
                "task_id": "2",
                "action": "add",
                "task": {
                    "subject": "Two",
                    "description": "Two",
                    "blocked_by": ["missing"],
                },
            },
        ],
    )

    assert response.state == ToolResultState.ERROR
    assert state.tasks_context.model_dump(mode="json") == before


def test_agent_replace_plan_is_the_edit_entry_point() -> None:
    class _Model(ChatModelBase):
        def __init__(self) -> None:
            self.model = "test"
            self.stream = False
            self.max_retries = 0
            self.context_size = 32768

        async def _call_api(self, *args, **kwargs):
            _ = (args, kwargs)
            return ChatResponse(
                content=[TextBlock(text="ok")],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )

    agent = QwenPawDataAgent(
        name="qwenpaw-data",
        system_prompt="",
        model=_Model(),
        toolkit=Toolkit(tools=[]),
        react_config=ReActConfig(max_iters=10),
    )
    context = agent.replace_plan(to_task_context(_plan_payload()), reason="edit")

    assert [task.id for task in context.tasks] == ["1", "2"]
    assert agent.get_plan() is context
    assert "修改原因：edit" in str(agent.state.context[-1])
    assert agent.replace_plan(TaskContext()).tasks == []
    assert agent.get_plan().tasks == []
