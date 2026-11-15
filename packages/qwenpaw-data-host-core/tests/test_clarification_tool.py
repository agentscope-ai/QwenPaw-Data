# -*- coding: utf-8 -*-
"""The native ask_user_question producer: advisory bounds and registration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw_data.host.core.agent.tools import AskUserQuestionTool
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.clarification import (
    ASK_USER_QUESTION,
    ClarificationWithFrontend,
)
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import ClarificationSettings, clear_settings

from .conftest import TEST_IDENTITY

_ENV_PREFIX = "QWENPAW_DATA_CLARIFICATION_"


def test_tool_is_external_so_the_executor_can_park() -> None:
    tool = AskUserQuestionTool()

    assert tool.name == ASK_USER_QUESTION
    assert tool.is_external_tool is True
    assert tool.is_read_only is True
    assert tool.is_concurrency_safe is False


def test_schema_carries_advisory_default_bounds() -> None:
    schema = AskUserQuestionTool.build_input_schema()

    questions = schema["properties"]["questions"]
    assert "prefer 1..4 items" in questions["description"]
    options = schema["$defs"]["_AskUserQuestionQuestion"]["properties"]["options"]
    assert "Prefer 2..4 items" in options["description"]
    assert not {"minItems", "maxItems"} & questions.keys()
    assert not {"minItems", "maxItems"} & options.keys()
    assert schema["required"] == ["title", "questions"]
    assert schema["additionalProperties"] is False


def test_schema_bounds_follow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{_ENV_PREFIX}QUESTIONS_MAX_ITEMS", "2")
    monkeypatch.setenv(f"{_ENV_PREFIX}OPTIONS_MAX_ITEMS", "6")
    clear_settings()

    schema = AskUserQuestionTool().input_schema

    questions = schema["properties"]["questions"]
    assert "1..2 items" in questions["description"]
    options = schema["$defs"]["_AskUserQuestionQuestion"]["properties"]["options"]
    assert "2..6 items" in options["description"]
    assert not {"minItems", "maxItems"} & questions.keys()
    assert not {"minItems", "maxItems"} & options.keys()


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "", "   "])
def test_unusable_env_is_rejected(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{_ENV_PREFIX}QUESTIONS_MAX_ITEMS", raw)

    with pytest.raises(ValueError, match="questions_max_items"):
        ClarificationSettings()


def test_inverted_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="questions_max_items"):
        ClarificationSettings(questions_min_items=3, questions_max_items=2)
    with pytest.raises(ValueError, match="options_max_items"):
        ClarificationSettings(options_min_items=4, options_max_items=2)


def test_hint_ttl_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setenv(f"{_ENV_PREFIX}HINT_TTL_SECONDS", "60")
    clear_settings()

    metadata = ClarificationWithFrontend.tool_call_metadata(
        now=now,
    )

    assert metadata == {"expires_at": "2026-01-01T00:01:00Z"}


def test_description_forbids_plain_text_questions() -> None:
    description = AskUserQuestionTool.description

    assert "REQUIRED" in description
    assert "status=timeout" in description


class _FakeWorkspace:
    async def list_tools(self) -> list:
        return []

    async def list_mcps(self, *, agent_id: str, session_id: str) -> list:
        return []

    async def list_skills(self, *, agent_id: str) -> list:
        return []


async def _tool_names() -> set[str]:
    context = RunContext(
        session_id="session-a",
        chat_id="chat-a",
        workspace=_FakeWorkspace(),
        paths=UserAgentPaths.from_identity(
            home=Path("."),
            identity=TEST_IDENTITY,
            agent_id="agent-a",
            session_id="session-a",
        ),
        user_runtime_config=SimpleNamespace(
            tool_timeout_enabled=True,
            tool_timeout_seconds=1800,
        ),
        identity=TEST_IDENTITY,
        session_factory=get_session_factory(),
    )
    toolkit = await DataAgentBuilder().build_toolkit(run_context=context)
    return {schema["function"]["name"] for schema in await toolkit.get_tool_schemas()}


@pytest.mark.parametrize("delegation", ["true", "false"])
async def test_registered_in_native_builder(
    delegation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", delegation)

    names = await _tool_names()

    assert ASK_USER_QUESTION in names


@pytest.mark.parametrize("ui_enabled", ["true", "false"])
async def test_registration_is_independent_of_ui_tool_flag(
    ui_enabled: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless chats use the executor's automatic answer, not tool removal."""
    monkeypatch.setenv("QWENPAW_DATA_TOOL_UI_ENABLED", ui_enabled)
    clear_settings()

    names = await _tool_names()

    assert ASK_USER_QUESTION in names
