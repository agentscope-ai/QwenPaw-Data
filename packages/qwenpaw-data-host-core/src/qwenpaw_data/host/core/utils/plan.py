# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from agentscope.state import Task, TaskContext


def normalize_tasks(tasks: list[Task]) -> list[Task]:
    """Validate task identity/references and rebuild reverse dependencies."""
    normalized = [task.model_copy(deep=True) for task in tasks]
    task_by_id: dict[str, Task] = {}
    for task in normalized:
        if task.id in task_by_id:
            raise ValueError(f"duplicate task id: {task.id}")
        task_by_id[task.id] = task

    for task in normalized:
        blocked_by: list[str] = []
        for dependency_id in task.blocked_by:
            if dependency_id not in task_by_id:
                raise ValueError(
                    f"task {task.id} references unknown dependency: {dependency_id}",
                )
            if dependency_id not in blocked_by:
                blocked_by.append(dependency_id)
        task.blocked_by = blocked_by
        task.blocks = []

    for task in normalized:
        for dependency_id in task.blocked_by:
            task_by_id[dependency_id].blocks.append(task.id)

    return normalized


def to_task_context(plan: dict[str, Any] | None) -> TaskContext:
    if plan is None:
        return TaskContext()
    tasks: list[Task] = []
    for task in plan.get("tasks", []):
        payload = dict(task)
        payload.setdefault("metadata", {})
        tasks.append(Task.model_validate(payload))
    return TaskContext(tasks=normalize_tasks(tasks))
