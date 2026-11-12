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
    "Ask the user clarifying multiple-choice questions to gather information, "
    "resolve ambiguity, understand preferences, confirm plans, or offer "
    "choices. REQUIRED for any clarifying question to the user: do NOT ask "
    "these as plain assistant text, numbered lists, or markdown option "
    "bullets — call this tool instead and wait for the result. "
    "Do not include an '其他' / 'Other' option in options. "
    "The tool result has status=answered with answers. Each answer contains "
    "the original question, selected_options (the option labels selected by "
    "the user), and custom_text (the user's free-form answer or additional "
    "explanation; null when absent). selected_options and custom_text may both "
    "be present. A status=timeout result means the user did not answer before "
    "the clarification timed out."
)


class _AskUserQuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Option label.",
    )
    description: str | None = Field(
        default=None,
        description="Optional option description.",
    )


class _AskUserQuestionQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Question body.",
    )
    description: str = Field(
        default="",
        description="Optional clarification for the question.",
    )
    multi_select: bool = Field(
        alias="multiSelect",
        description="true for multiple-choice; false for single-choice.",
    )
    options: list[_AskUserQuestionOption] = Field(
        description=(
            "Concrete, self-contained choices only; selecting one must be "
            "enough to continue — do not ask the user to type extra facts. "
            "Do not include '其他' / 'Other'. Prefer "
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
