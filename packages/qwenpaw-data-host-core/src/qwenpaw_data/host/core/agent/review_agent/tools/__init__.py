# -*- coding: utf-8 -*-
"""Tools for inspecting and auditing the reviewed session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentscope.tool import ToolBase

from .artifact import GetArtifactsTool, ReadArtifactTool
from .base import ReviewToolBase
from .overview import GetOverviewTool
from .plan import GetPlanTool
from .requirement import ReviewRequirementTool
from .trace import GetTraceTool

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext

__all__ = [
    "GetArtifactsTool",
    "GetOverviewTool",
    "GetPlanTool",
    "GetTraceTool",
    "ReadArtifactTool",
    "ReviewRequirementTool",
    "ReviewToolBase",
    "build_review_tools",
]


def build_review_tools(run_context: RunContext) -> list[ToolBase]:
    """Review tools bound to one review run context."""
    return [
        GetOverviewTool(run_context),
        GetPlanTool(run_context),
        GetTraceTool(run_context),
        GetArtifactsTool(run_context),
        ReadArtifactTool(run_context),
        ReviewRequirementTool(run_context),
    ]
