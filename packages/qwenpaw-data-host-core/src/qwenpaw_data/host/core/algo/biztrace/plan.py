# -*- coding: utf-8 -*-
"""In-memory plan ledger: successful orchestration calls, in biz-seq order."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import BizEvent
from qwenpaw_data.host.core.algo.evaluation.settings import PRIOR_SEGMENTS


@dataclass(frozen=True, slots=True)
class PlanOp:
    """One successful plan tool call, positioned at the call card's biz seq."""

    seq: int
    name: str
    arguments: dict[str, Any]


class PlanLedger:
    """Observe BizEvents and replay the plan state at a given biz seq."""

    def __init__(self) -> None:
        self._pending: dict[str, PlanOp] = {}
        self._ops: list[PlanOp] = []

    def observe(self, event: BizEvent) -> None:
        """Hold a plan tool_use until its tool_result commits or drops it."""
        if event.kind == "tool_use" and event.orchestration is not None:
            arguments = event.input if isinstance(event.input, dict) else {}
            self._pending[event.event_id] = PlanOp(
                seq=event.seq,
                name=event.orchestration.category,
                arguments=arguments,
            )
            return
        if event.kind != "tool_result":
            return
        pending = self._pending.pop(event.block_id or "", None)
        if pending is None:
            return
        if event.status == "done":
            self._ops.append(pending)
            self._ops.sort(key=lambda op: op.seq)

    def snapshot(self, *, at: int) -> list[dict[str, Any]]:
        """The plan state at biz seq ``at``: tasks in creation order."""
        return list(replay_plan(self._ops, at=at).values())

    def active_ids(self, at: int) -> set[str]:
        """Tasks in progress at biz seq ``at``."""
        return {
            task_id
            for task_id, task in replay_plan(self._ops, at=at).items()
            if task["state"] == "in_progress"
        }


def replay_plan(ops: list[PlanOp], *, at: int) -> dict[str, dict[str, Any]]:
    """The plan state at biz seq ``at``: tasks by id, in creation order."""
    tasks: dict[str, dict[str, Any]] = {}
    for op in ops:
        if op.seq > at:
            break
        if op.name == "PlanCreate":
            created: dict[str, dict[str, Any]] = {}
            raw = op.arguments.get("tasks")
            for item in raw if isinstance(raw, list) else []:
                task = _plan_task(item)
                if task is not None:
                    created[task["id"]] = task
            tasks = created
        elif op.name == "PlanUpdate":
            raw = op.arguments.get("changes")
            for change in raw if isinstance(raw, list) else []:
                _apply_change(tasks, change)
        elif op.name == "TaskStateUpdate":
            task_id = str(op.arguments.get("task_id") or "")
            state = str(op.arguments.get("state") or "")
            if task_id in tasks and state:
                tasks[task_id]["state"] = state
    return tasks


def select_priors(
    rows: list[tuple[int, int, str]],
    *,
    before: int,
    subtask: str | None = None,
    active_at: Callable[[int], set[str]] | None = None,
    limit: int = PRIOR_SEGMENTS,
) -> list[tuple[int, str]]:
    """Numbered conclusions of segments that ended before biz seq ``before``."""
    eligible = [
        (end_seq, start_seq, conclusion)
        for end_seq, start_seq, conclusion in rows
        if conclusion and 0 <= end_seq < before
    ]
    eligible.sort(key=lambda row: row[0])
    numbered = [
        (ordinal, start_seq, conclusion)
        for ordinal, (_, start_seq, conclusion) in enumerate(eligible, start=1)
    ]
    if subtask is not None and active_at is not None:
        selected = [row for row in numbered if subtask in active_at(row[1])][-limit:]
    else:
        selected = numbered[-limit:]
    return [(ordinal, conclusion) for ordinal, _, conclusion in selected]


def _apply_change(tasks: dict[str, dict[str, Any]], change: Any) -> None:
    if not isinstance(change, dict):
        return
    task_id = str(change.get("task_id") or "")
    action = change.get("action")
    if not task_id:
        return
    if action == "delete":
        tasks.pop(task_id, None)
    elif action == "add":
        task = _plan_task(change.get("task"), task_id=task_id)
        if task is not None:
            tasks[task["id"]] = task
    elif action == "revise":
        if task_id not in tasks:
            return
        task = _plan_task(change.get("task"), task_id=task_id)
        if task is not None:
            tasks[task_id] = task


def _plan_task(item: Any, *, task_id: str | None = None) -> dict[str, Any] | None:
    """One task dict (id/subject/description/state), pending by default."""
    if not isinstance(item, dict):
        return None
    resolved = task_id or str(item.get("id") or "").strip()
    if not resolved:
        return None
    return {
        "id": resolved,
        "subject": str(item.get("subject") or ""),
        "description": str(item.get("description") or ""),
        "state": str(item.get("state") or "pending"),
    }


__all__ = [
    "PlanLedger",
    "PlanOp",
    "replay_plan",
    "select_priors",
]
