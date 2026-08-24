# -*- coding: utf-8 -*-
"""QwenPaw Data ToolBase 子类 —— agentscope 2.0 工具协议实现。

每个工具委托到 RuntimeStateManager 对应的异步方法。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from .state import RuntimeStateManager


class QwenPawDataToolBase(ToolBase):
    """QwenPaw Data 工具基类。构造时接收 RuntimeStateManager 引用。"""

    is_state_injected: bool = False
    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(self, runtime_state: RuntimeStateManager) -> None:
        self._rs = runtime_state

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed.",
        )


class CreatePlan(QwenPawDataToolBase):
    name: str = "create_plan"
    description: str = (
        "Create a new analysis task graph (DAG). Use this when the user's "
        "request requires multiple analytical steps."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short, descriptive graph name (<= 10 words).",
            },
            "description": {
                "type": "string",
                "description": "Constraints, target, and measurable outcome.",
            },
            "expected_outcome": {
                "type": "string",
                "description": "Specific, concrete final deliverable.",
            },
            "nodes": {
                "type": "array",
                "description": (
                    "List of node dicts. Each must have name, description, "
                    "expected_outcome, and optionally node_id and deps. "
                    "deps must reference node_id values, not node names."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": (
                                "Stable node id used by downstream deps."
                            ),
                        },
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "deps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Upstream node_id values only; do not use node "
                                "names."
                            ),
                        },
                    },
                    "required": ["name", "description", "expected_outcome"],
                },
            },
        },
        "required": ["name", "description", "expected_outcome", "nodes"],
    }

    async def __call__(  # type: ignore[override]
        self,
        name: str,
        description: str,
        expected_outcome: str,
        nodes: List[Dict[str, Any]],
    ) -> ToolChunk:
        return await self._rs.create_plan(name, description, expected_outcome, nodes)


class UpdateSubtask(QwenPawDataToolBase):
    name: str = "update_subtask"
    description: str = (
        "Update the status of a DAG node. Use 'in_progress' to start "
        "working, 'done' to finish with output, 'todo' to reset, "
        "'failed'/'abandoned' for terminal error states."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "ID of the node to update.",
            },
            "state": {
                "type": "string",
                "enum": [
                    "todo", "in_progress", "done",
                    "failed", "abandoned",
                ],
                "description": "Target state for the node.",
            },
            "reasoning": {
                "type": "string",
                "description": "How the work was done (required when state='done').",
            },
            "summary": {
                "type": "string",
                "description": "What the result is (required when state='done').",
            },
            "files": {
                "description": "Optional list of FileRef objects (for state='done').",
                "oneOf": [
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "path": {"type": "string"},
                                "mime_type": {"type": "string"},
                            },
                            "required": ["name", "path", "mime_type"],
                        },
                    },
                    {"type": "null"},
                ],
            },
        },
        "required": ["node_id", "state"],
    }

    async def __call__(  # type: ignore[override]
        self,
        node_id: str,
        state: str,
        reasoning: Optional[str] = None,
        summary: Optional[str] = None,
        files: Any = None,
    ) -> ToolChunk:
        return await self._rs.update_subtask(
            node_id, state,
            reasoning=reasoning, summary=summary, files=files,
        )


class ReviseCurrentPlan(QwenPawDataToolBase):
    name: str = "revise_current_plan"
    description: str = (
        "Atomically add / revise / delete nodes in the active graph. "
        "Pass every intended mutation in one changes array. Revised nodes "
        "and their downstream nodes are reset to todo."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": (
                    "Batch of node changes. The whole batch is atomic: if any "
                    "change has invalid deps, unknown nodes, or a cycle, "
                    "nothing is modified."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Target node_id.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["add", "revise", "delete"],
                            "description": "'add' / 'revise' / 'delete'.",
                        },
                        "node": {
                            "type": "object",
                            "description": (
                                "Required for 'add' and 'revise'. This is a "
                                "full replacement node; include direct "
                                "upstream node_id deps, or [] for leaf nodes."
                            ),
                            "properties": {
                                "node_id": {
                                    "type": "string",
                                    "description": (
                                        "Optional for add/revise. If provided "
                                        "for add, it must match the outer "
                                        "change.node_id."
                                    ),
                                },
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "expected_outcome": {"type": "string"},
                                "deps": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Direct upstream node_id values only; "
                                        "do not include transitive ancestors."
                                    ),
                                },
                            },
                            "required": [
                                "name",
                                "description",
                                "expected_outcome",
                                "deps",
                            ],
                        },
                    },
                    "required": ["node_id", "action"],
                },
            },
        },
        "required": ["changes"],
    }

    async def __call__(  # type: ignore[override]
        self,
        changes: List[Dict[str, Any]],
    ) -> ToolChunk:
        return await self._rs.revise_current_plan(changes)


class FinishPlan(QwenPawDataToolBase):
    name: str = "finish_plan"
    description: str = (
        "Finish the active graph with an outcome. Call after all nodes "
        "are done, or use 'abandoned' to abort."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["done", "abandoned"],
                "description": "'done' or 'abandoned'.",
            },
            "outcome": {
                "type": "string",
                "description": "Final report summary.",
            },
        },
        "required": ["state", "outcome"],
    }

    async def __call__(  # type: ignore[override]
        self,
        state: str,
        outcome: str,
    ) -> ToolChunk:
        return await self._rs.finish_plan(state, outcome)


PLAN_MODE_TOOL_NAMES = {"create_plan", "revise_current_plan"}

ALL_QWENPAW_DATA_TOOLS = (
    CreatePlan,
    UpdateSubtask,
    ReviseCurrentPlan,
    FinishPlan,
)


def build_qwenpaw_data_tools(
    runtime_state: RuntimeStateManager,
    mode: str = "agent",
) -> list[ToolBase]:
    """Build QwenPaw Data tool instances for the given mode."""
    tools = [cls(runtime_state) for cls in ALL_QWENPAW_DATA_TOOLS]
    if mode == "plan":
        tools = [t for t in tools if t.name in PLAN_MODE_TOOL_NAMES]
    return tools
