# -*- coding: utf-8 -*-
"""Clarification domain: executor-facing pause/resume for ask_user_question."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from agentscope.message import TextBlock, ToolResultBlock, ToolResultState
from pydantic import BaseModel, Field, model_validator

ASK_USER_QUESTION = "ask_user_question"

_ENV_PREFIX = "QWENPAW_DATA_CLARIFICATION_"

DEFAULT_HINT_TTL_SECONDS = 300


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"{_ENV_PREFIX}{name.upper()}")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class ClarificationSettings(BaseModel):
    """Bounds for one ask_user_question card; every knob has a safe default.

    Defaults come from ``QWENPAW_DATA_CLARIFICATION_*`` env vars; constructor
    kwargs win.
    """

    hint_ttl_seconds: int = Field(
        default_factory=lambda: _env_int("hint_ttl_seconds", DEFAULT_HINT_TTL_SECONDS)
    )
    questions_min_items: int = Field(
        default_factory=lambda: _env_int("questions_min_items", 1)
    )
    questions_max_items: int = Field(
        default_factory=lambda: _env_int("questions_max_items", 4)
    )
    options_min_items: int = Field(
        default_factory=lambda: _env_int("options_min_items", 2)
    )
    options_max_items: int = Field(
        default_factory=lambda: _env_int("options_max_items", 4)
    )

    @model_validator(mode="after")
    def _validate(self) -> ClarificationSettings:
        if self.questions_max_items < self.questions_min_items:
            raise ValueError("questions_max_items must be >= questions_min_items")
        if self.options_max_items < self.options_min_items:
            raise ValueError("options_max_items must be >= options_min_items")
        return self


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


class ClarificationWithLLM:
    """LLM ↔ Host: tool registration and the AgentScope input schema."""

    @staticmethod
    def tool_name() -> str:
        return ASK_USER_QUESTION

    @staticmethod
    def tool_description() -> str:
        return _TOOL_DESCRIPTION

    @staticmethod
    def agent_input_schema(
        settings: ClarificationSettings | None = None,
    ) -> dict[str, Any]:
        """Build the tool's JSON Schema with settings-driven item bounds."""
        cfg = settings or ClarificationSettings()
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"\S",
                    "description": "Card title shown to the user.",
                },
                "questions": {
                    "type": "array",
                    "description": (
                        "Questions to ask; must contain "
                        f"{cfg.questions_min_items}..{cfg.questions_max_items} items."
                    ),
                    "minItems": cfg.questions_min_items,
                    "maxItems": cfg.questions_max_items,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "minLength": 1,
                                "pattern": r"\S",
                                "description": "Question body.",
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "Optional clarification for the question."
                                ),
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": (
                                    "true for multiple-choice; false for "
                                    "single-choice."
                                ),
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    "Concrete, self-contained choices only; "
                                    "selecting one must be enough to continue — "
                                    "do not ask the user to type extra facts. "
                                    "Do not include '其他' / 'Other'. Must contain "
                                    f"{cfg.options_min_items}.."
                                    f"{cfg.options_max_items} items."
                                ),
                                "minItems": cfg.options_min_items,
                                "maxItems": cfg.options_max_items,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "minLength": 1,
                                            "pattern": r"\S",
                                            "description": "Option label.",
                                        },
                                        "description": {
                                            "type": ["string", "null"],
                                            "description": (
                                                "Optional option description."
                                            ),
                                        },
                                    },
                                    "required": ["label"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["question", "multiSelect", "options"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "questions"],
            "additionalProperties": False,
        }


class ClarificationConflict(RuntimeError):
    """HTTP-mappable conflict for clarification REST."""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"CONFLICT: {message}")


class ClarificationNotFound(LookupError):
    def __init__(self, message: str = "clarification not found") -> None:
        self.reason = "CLARIFICATION_NOT_FOUND"
        super().__init__(message)


class ClarificationWithFrontend:
    """Host ↔ Frontend: Clarification interaction metadata."""

    @staticmethod
    def tool_call_metadata(
        *,
        now: datetime | None = None,
        settings: ClarificationSettings | None = None,
    ) -> dict[str, str]:
        cfg = settings or ClarificationSettings()
        expires_at = (now or datetime.now(UTC)) + timedelta(
            seconds=cfg.hint_ttl_seconds
        )
        return {
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }


class ClarificationWithExecutor:
    """Executor-facing pause/resume for one ask_user_question."""

    def __init__(self) -> None:
        self._call_id: str | None = None
        self._result: asyncio.Future[dict[str, Any]] | None = None

    @property
    def call_id(self) -> str | None:
        return self._call_id

    @property
    def is_pending(self) -> bool:
        return self._result is not None and not self._result.done()

    @staticmethod
    def is_timeout(tool_result: ToolResultBlock) -> bool:
        output = tool_result.output[0]
        text = output.text if isinstance(output, TextBlock) else str(output)
        return json.loads(text).get("status") == "timeout"

    @staticmethod
    def _to_tool_result_block(
        *,
        call_id: str,
        result: dict[str, Any],
    ) -> ToolResultBlock:
        return ToolResultBlock(
            id=call_id,
            name=ASK_USER_QUESTION,
            output=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    def add_metadata(
        self,
        *,
        tool_name: str,
        metadata: dict[str, Any],
    ) -> None:
        if tool_name != ASK_USER_QUESTION:
            return
        metadata.update(ClarificationWithFrontend.tool_call_metadata())

    def answer(
        self,
        *,
        clarification_id: str,
        result: dict[str, Any],
    ) -> None:
        future = self._result
        if future is None or self._call_id is None:
            raise ClarificationNotFound()
        if self._call_id != clarification_id or future.done():
            raise ClarificationConflict(
                "clarification is not awaiting this answer",
                reason="CLARIFICATION_ALREADY_RESOLVED",
            )
        future.set_result(result)

    async def wait_for_answer(self, call_id: str) -> ToolResultBlock:
        """Park until ``answer`` delivers a result for ``call_id``."""
        if not call_id.strip():
            raise ValueError("call_id is required")
        self._call_id = call_id
        self._result = asyncio.get_running_loop().create_future()
        try:
            payload = await self._result
            return self._to_tool_result_block(call_id=call_id, result=payload)
        finally:
            self._call_id = None
            self._result = None
