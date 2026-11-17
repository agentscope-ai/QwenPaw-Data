# -*- coding: utf-8 -*-
"""ask_user_question as an AgentScope external tool."""

from __future__ import annotations

from typing import Any

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.settings import get_settings

_QUESTIONS_MIN = "__CLARIFICATION_QUESTIONS_MIN_ITEMS__"
_QUESTIONS_MAX = "__CLARIFICATION_QUESTIONS_MAX_ITEMS__"
_OPTIONS_MIN = "__CLARIFICATION_OPTIONS_MIN_ITEMS__"
_OPTIONS_MAX = "__CLARIFICATION_OPTIONS_MAX_ITEMS__"

_TOOL_DESCRIPTION = (
    "Ask one closed multiple-choice question. Every option is a concrete value "
    "already found by probing, or the plan-confirmation action; selecting it "
    "is enough to continue. Put those choices here, not in a markdown list. "
    "Do not call it when the user must type the answer (an ID, name, project, "
    "connection, or free text), or when no option can unblock the task "
    "(uninitialized graph space, missing permission, service down). Say that "
    "in the reply and end the turn. "
    "No '其他' / 'Other', and no meta option such as '我知道 X', '我不确定', "
    "or '提供其他方式'. "
    "Result status=answered: each answer has the question, selected_options, "
    "and custom_text (an optional supplement, null if absent — not the "
    "intended answer). status=timeout means the user did not answer in time."
)


class _AskUserQuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Concrete value or action, selectable as-is.",
    )
    description: str | None = Field(
        default=None,
        description="Optional note on this option.",
    )


class _AskUserQuestionQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Must be answerable by selecting an option alone.",
    )
    description: str = Field(
        default="",
        description="Optional background.",
    )
    multi_select: bool = Field(
        alias="multiSelect",
        description="true for multiple-choice; false for single-choice.",
    )
    options: list[_AskUserQuestionOption] = Field(
        description=(
            "Self-contained choices from probe results; selecting one is "
            "enough. Do not include '其他' / 'Other'. Prefer "
            f"{_OPTIONS_MIN}..{_OPTIONS_MAX} items."
        ),
    )


class _AskUserQuestionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Card title shown to the user.",
    )
    questions: list[_AskUserQuestionQuestion] = Field(
        description=(
            f"Questions to ask; prefer {_QUESTIONS_MIN}..{_QUESTIONS_MAX} items."
        ),
    )


class AskUserQuestionTool(ToolBase):
    """External tool: Host pauses on RequireExternalExecutionEvent."""

    name = "ask_user_question"
    description = _TOOL_DESCRIPTION
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(self) -> None:
        super().__init__()
        # Build from settings at construction time (not import time).
        self.input_schema = self.build_input_schema()

    @staticmethod
    def build_input_schema() -> dict[str, Any]:
        settings = get_settings().clarification
        schema = _AskUserQuestionParams.model_json_schema()
        replacements: dict[str, int] = {
            _QUESTIONS_MIN: settings.questions_min_items,
            _QUESTIONS_MAX: settings.questions_max_items,
            _OPTIONS_MIN: settings.options_min_items,
            _OPTIONS_MAX: settings.options_max_items,
        }
        questions = schema["properties"]["questions"]
        questions["description"] = (
            questions["description"]
            .replace(_QUESTIONS_MIN, str(replacements[_QUESTIONS_MIN]))
            .replace(_QUESTIONS_MAX, str(replacements[_QUESTIONS_MAX]))
        )
        question = schema["$defs"]["_AskUserQuestionQuestion"]
        options = question["properties"]["options"]
        options["description"] = (
            options["description"]
            .replace(_OPTIONS_MIN, str(replacements[_OPTIONS_MIN]))
            .replace(_OPTIONS_MAX, str(replacements[_OPTIONS_MAX]))
        )
        return schema

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
