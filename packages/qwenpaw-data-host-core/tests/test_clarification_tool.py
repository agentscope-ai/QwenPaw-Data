# -*- coding: utf-8 -*-
"""The ask_user_question producer: schema bounds, flags, and registration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qwenpaw_data.host.core.agent.tools import AskUserQuestionTool
from qwenpaw_data.host.core.agent.toolkit import build_qwenpaw_data_toolkit
from qwenpaw_data.host.core.domain.clarification import (
    ASK_USER_QUESTION,
    ClarificationSettings,
    ClarificationWithFrontend,
    ClarificationWithLLM,
)
from qwenpaw_data.host.core.orchestration import RuntimeStateManager

_ENV_PREFIX = "QWENPAW_DATA_CLARIFICATION_"


def test_tool_is_external_so_the_executor_can_park() -> None:
    tool = AskUserQuestionTool()

    assert tool.name == ASK_USER_QUESTION
    assert tool.is_external_tool is True
    assert tool.is_read_only is True
    assert tool.is_concurrency_safe is False


def test_schema_carries_default_bounds() -> None:
    schema = ClarificationWithLLM.agent_input_schema()

    questions = schema["properties"]["questions"]
    assert (questions["minItems"], questions["maxItems"]) == (1, 4)
    options = questions["items"]["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (2, 4)
    assert schema["required"] == ["title", "questions"]
    assert schema["additionalProperties"] is False


def test_schema_bounds_follow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{_ENV_PREFIX}QUESTIONS_MAX_ITEMS", "2")
    monkeypatch.setenv(f"{_ENV_PREFIX}OPTIONS_MAX_ITEMS", "6")

    schema = AskUserQuestionTool().input_schema

    questions = schema["properties"]["questions"]
    assert questions["maxItems"] == 2
    assert "1..2 items" in questions["description"]
    options = questions["items"]["properties"]["options"]
    assert options["maxItems"] == 6
    assert "2..6 items" in options["description"]


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "", "   "])
def test_unusable_env_falls_back_to_default(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{_ENV_PREFIX}QUESTIONS_MAX_ITEMS", raw)

    assert ClarificationSettings().questions_max_items == 4


def test_inverted_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="questions_max_items"):
        ClarificationSettings(questions_min_items=3, questions_max_items=2)
    with pytest.raises(ValueError, match="options_max_items"):
        ClarificationSettings(options_min_items=4, options_max_items=2)


def test_hint_ttl_is_configurable() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    metadata = ClarificationWithFrontend.tool_call_metadata(
        now=now,
        settings=ClarificationSettings(hint_ttl_seconds=60),
    )

    assert metadata == {"expires_at": "2026-01-01T00:01:00Z"}


def test_description_forbids_plain_text_questions() -> None:
    description = ClarificationWithLLM.tool_description()

    assert "REQUIRED" in description
    assert "status=timeout" in description


class _FakeWorkspace:
    async def list_tools(self) -> list:
        return []

    async def list_mcps(self) -> list:
        return []

    async def list_skills(self) -> list:
        return []


async def _tool_names(*, enable_clarification: bool, mode: str) -> set[str]:
    toolkit = await build_qwenpaw_data_toolkit(
        RuntimeStateManager(),
        workspace=_FakeWorkspace(),
        enable_clarification=enable_clarification,
    )
    toolkit.set_qwenpaw_data_mode(mode)
    return {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas([mode])
    }


@pytest.mark.parametrize("mode", ["plan", "agent"])
async def test_registered_in_both_modes_when_enabled(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)

    names = await _tool_names(enable_clarification=True, mode=mode)

    assert ASK_USER_QUESTION in names


@pytest.mark.parametrize("mode", ["plan", "agent"])
async def test_absent_when_no_executor_can_service_it(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI-style hosts leave it off, or the turn would park forever."""
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)

    names = await _tool_names(enable_clarification=False, mode=mode)

    assert ASK_USER_QUESTION not in names
