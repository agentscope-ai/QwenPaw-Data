# -*- coding: utf-8 -*-
"""QwenPawData Plan tools backed by AgentScope state injection."""

from __future__ import annotations

from typing import Any, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.state import AgentState, Task, TaskContext
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.utils.plan import normalize_tasks


PLAN_TOOL_NAMES = frozenset({"PlanCreate", "PlanUpdate", "TaskStateUpdate"})
TaskState = Literal["pending", "in_progress", "completed"]
PlanChangeAction = Literal["add", "revise", "delete"]


class PlanCreateTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    description: str
    owner: str | None = None
    blocked_by: list[str] = Field(default_factory=list)


class PlanTaskRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    description: str
    owner: str | None = None
    blocked_by: list[str] = Field(default_factory=list)


class PlanTaskChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    action: PlanChangeAction
    task: PlanTaskRevision | None = Field(
        default=None,
        description=(
            "Required for add/revise; omit or pass null for delete. "
            "For add/revise, put subject and description inside task, "
            "not alongside task_id and action."
        ),
    )


class _PlanCreateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[PlanCreateTaskInput]


class _PlanUpdateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[PlanTaskChange]


class _TaskStateUpdateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: TaskState


class _QwenPawDataPlanTool(ToolBase):
    is_state_injected = True
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False
    is_mcp = False
    mcp_name = None

    def __init__(self) -> None:
        super().__init__()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        _ = (tool_input, context)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed.",
        )

    @staticmethod
    def _state(value: Any) -> AgentState:
        if not isinstance(value, AgentState):
            raise TypeError("QwenPawData Plan tools require AgentState")
        return value

    @staticmethod
    def _success(message: str) -> ToolChunk:
        return ToolChunk(content=[TextBlock(text=message)])

    @staticmethod
    def _error(exc: Exception) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(text=f"PlanError: {exc}")],
            state=ToolResultState.ERROR,
        )


class PlanCreate(_QwenPawDataPlanTool):
    name = "PlanCreate"
    description = (
        "Create a complete Plan and replace any current Plan atomically. "
        "All tasks start in pending state. Use stable task IDs and express "
        "dependencies with blocked_by."
    )
    input_schema = _PlanCreateParams.model_json_schema()

    async def call(
        self,
        _agent_state: AgentState,
        tasks: list[dict[str, Any]],
    ) -> ToolChunk:
        try:
            state = self._state(_agent_state)
            state.tasks_context = self.build_context(tasks)
            return self._success(
                f"Plan created with {len(state.tasks_context.tasks)} task(s).",
            )
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def build_context(
        tasks: list[PlanCreateTaskInput | dict[str, Any]],
    ) -> TaskContext:
        definitions = [
            task
            if isinstance(task, PlanCreateTaskInput)
            else PlanCreateTaskInput.model_validate(task)
            for task in tasks
        ]
        return TaskContext(
            tasks=normalize_tasks(
                [
                    Task(
                        id=task.id,
                        subject=task.subject,
                        description=task.description,
                        metadata={},
                        state="pending",
                        owner=task.owner,
                        blocked_by=list(task.blocked_by),
                    )
                    for task in definitions
                ],
            ),
        )


class PlanUpdate(_QwenPawDataPlanTool):
    name = "PlanUpdate"
    description = (
        "Atomically apply a batch of task add, revise, or delete changes to "
        "the current Plan. Each change must include task_id and action. "
        "For add/revise, task is required and must contain subject and description; "
        "do not put these fields alongside task_id and action. "
        "For delete, provide only task_id and action. "
        "Revised tasks return to pending; unchanged tasks keep their state. "
        'Example: {"changes":[{"task_id":"fetch_data","action":"revise",'
        '"task":{"subject":"Fetch data","description":"Fetch the last 7 days"}}]}'
    )
    input_schema = _PlanUpdateParams.model_json_schema()

    async def call(
        self,
        _agent_state: AgentState,
        changes: list[dict[str, Any]],
    ) -> ToolChunk:
        try:
            state = self._state(_agent_state)
            state.tasks_context = self.apply_changes(
                state.tasks_context,
                changes=changes,
            )
            return self._success(f"Plan updated with {len(changes)} change(s).")
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def apply_changes(
        current: TaskContext,
        *,
        changes: list[PlanTaskChange | dict[str, Any]],
    ) -> TaskContext:
        if not current.tasks:
            raise ValueError("no current plan")

        parsed_changes = [
            change
            if isinstance(change, PlanTaskChange)
            else PlanTaskChange.model_validate(change)
            for change in changes
        ]
        change_ids = [change.task_id for change in parsed_changes]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("duplicate task_id in PlanUpdate changes")

        tasks = [task.model_copy(deep=True) for task in current.tasks]
        for change in parsed_changes:
            task_index = next(
                (
                    index
                    for index, task in enumerate(tasks)
                    if task.id == change.task_id
                ),
                None,
            )

            if change.action == "add":
                if task_index is not None:
                    raise ValueError(f"task already exists: {change.task_id}")
                if change.task is None:
                    raise ValueError(
                        f"task is required for add: {change.task_id}. "
                        "Put subject and description inside changes[i].task.",
                    )
                tasks.append(
                    Task(
                        id=change.task_id,
                        subject=change.task.subject,
                        description=change.task.description,
                        metadata={},
                        state="pending",
                        owner=change.task.owner,
                        blocked_by=list(change.task.blocked_by),
                    ),
                )

            elif change.action == "revise":
                if task_index is None:
                    raise ValueError(f"task not found: {change.task_id}")
                if change.task is None:
                    raise ValueError(
                        f"task is required for revise: {change.task_id}. "
                        "Put subject and description inside changes[i].task.",
                    )
                previous = tasks[task_index]
                tasks[task_index] = Task(
                    id=previous.id,
                    subject=change.task.subject,
                    description=change.task.description,
                    metadata=previous.metadata.copy(),
                    created_at=previous.created_at,
                    state="pending",
                    owner=change.task.owner,
                    blocked_by=list(change.task.blocked_by),
                )

            else:
                if task_index is None:
                    raise ValueError(f"task not found: {change.task_id}")
                tasks.pop(task_index)
                for task in tasks:
                    task.blocked_by = [
                        dependency_id
                        for dependency_id in task.blocked_by
                        if dependency_id != change.task_id
                    ]

        return TaskContext(tasks=normalize_tasks(tasks))


class TaskStateUpdate(_QwenPawDataPlanTool):
    name = "TaskStateUpdate"
    description = (
        "Update only one current Plan task state. Dependencies are advisory "
        "and do not prevent pending, in_progress, or completed updates."
    )
    input_schema = _TaskStateUpdateParams.model_json_schema()

    async def call(
        self,
        _agent_state: AgentState,
        task_id: str,
        state: TaskState,
    ) -> ToolChunk:
        try:
            agent_state = self._state(_agent_state)
            agent_state.tasks_context = self.apply_state(
                agent_state.tasks_context,
                task_id=task_id,
                state=state,
            )
            return self._success(f"Task {task_id} state updated to {state}.")
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def apply_state(
        current: TaskContext,
        *,
        task_id: str,
        state: TaskState,
    ) -> TaskContext:
        if not current.tasks:
            raise ValueError("no current plan")

        updated = current.model_copy(deep=True)
        task = next((task for task in updated.tasks if task.id == task_id), None)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        task.state = state
        return TaskContext(tasks=normalize_tasks(updated.tasks))


def build_plan_tools() -> list[ToolBase]:
    return [PlanCreate(), PlanUpdate(), TaskStateUpdate()]
