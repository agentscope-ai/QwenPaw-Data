# -*- coding: utf-8 -*-
"""``ask_user_question`` as an AgentScope external tool.

The tool never executes in-process: AgentScope raises
``RequireExternalExecutionEvent``, which :class:`AgentExecutor` parks on until
the REST layer delivers the user's answer.
"""

from __future__ import annotations

from typing import Any

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase

from ...domain.clarification import ClarificationSettings, ClarificationWithLLM


class AskUserQuestionTool(ToolBase):
    """External tool: the host pauses the turn and waits for the user."""

    name = ClarificationWithLLM.tool_name()
    description = ClarificationWithLLM.tool_description()
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(self, settings: ClarificationSettings | None = None) -> None:
        super().__init__()
        # Built per instance so env changes apply without a process restart.
        self.input_schema = ClarificationWithLLM.agent_input_schema(settings)

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
