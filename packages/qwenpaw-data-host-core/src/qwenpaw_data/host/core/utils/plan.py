# -*- coding: utf-8 -*-
"""Project between the runtime SOP plan shape and the console Plan schema.

The console client edits plans as ``{"tasks": [...]}`` (subject/state/
blocked_by); the runtime plans in SOP shape (name/description/
expected_outcome/deps). Chats persist SOP dumps, optionally annotated
with per-node ``state`` from the live graph.
"""

from __future__ import annotations

from typing import Any

_NODE_STATE_TO_TASK = {
    "todo": "pending",
    "in_progress": "in_progress",
    "done": "completed",
    "failed": "in_progress",
    "abandoned": "completed",
}


def sop_plan_to_schema(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """SOP dump (nodes may carry ``state``) → console Plan dict, or None."""
    if not plan:
        return None
    nodes = plan.get("nodes") or []
    if not nodes:
        return None
    tasks: list[dict[str, Any]] = []
    blocks: dict[str, list[str]] = {}
    for node in nodes:
        for dep in node.get("deps") or []:
            blocks.setdefault(dep, []).append(node.get("node_id") or "")
    for index, node in enumerate(nodes):
        node_id = node.get("node_id") or f"node_{index:03d}"
        metadata: dict[str, Any] = {}
        if node.get("expected_outcome"):
            metadata["expected_outcome"] = node["expected_outcome"]
        tasks.append(
            {
                "id": node_id,
                "subject": node.get("name") or node_id,
                "description": node.get("description") or "",
                "metadata": metadata,
                "state": _NODE_STATE_TO_TASK.get(
                    node.get("state") or "todo", "pending"
                ),
                "owner": None,
                "blocks": blocks.get(node_id, []),
                "blocked_by": list(node.get("deps") or []),
            }
        )
    return {"tasks": tasks}


def plan_schema_to_sop(
    plan: dict[str, Any] | None,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Console Plan dict → SOP dict; graph meta carried over from ``previous``."""
    if plan is None:
        return None
    prior = previous or {}
    nodes = [
        {
            "node_id": task["id"],
            "name": task.get("subject") or task["id"],
            "description": task.get("description") or "",
            "expected_outcome": (
                (task.get("metadata") or {}).get("expected_outcome")
                or task.get("description")
                or task.get("subject")
                or task["id"]
            ),
            "deps": list(task.get("blocked_by") or []),
        }
        for task in plan.get("tasks") or []
    ]
    return {
        "name": prior.get("name") or "用户编辑的计划",
        "description": prior.get("description") or "由控制台计划编辑生成",
        "expected_outcome": prior.get("expected_outcome") or "按编辑后的计划完成任务",
        "nodes": nodes,
    }
