# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw_data.host.core.agent.tools.plan_tools import PLAN_TOOL_NAMES
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo
from qwenpaw_data.host.core.runtime.builder import (
    DataAgentBuilder,
    ReviewAgentBuilder,
    get_agent_builder,
)
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.workspace.workspace_manager import prepare_workspace_dirs

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class FakeTool:
    description = "fake"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(self, name: str, *, is_mcp: bool = False) -> None:
        self.name = name
        self.is_mcp = is_mcp
        self.mcp_name = "fake-mcp" if is_mcp else None


class FakeMCP:
    name = "fake-mcp"
    is_stateful = False
    is_connected = False
    mcp_config = {
        "type": "http_mcp",
        "url": "http://example.test/mcp",
        "headers": {},
        "timeout": 30.0,
    }
    execution_timeout = None

    async def list_tools(self) -> list[FakeTool]:
        return [FakeTool("mcp__fake-mcp__execute_sql", is_mcp=True)]


class FakeWorkspace:
    async def list_tools(self) -> list[FakeTool]:
        return [
            FakeTool("Read"),
            FakeTool("Write"),
            FakeTool("Bash"),
            FakeTool("PowerShell"),
        ]

    async def list_mcps(self) -> list[FakeMCP]:
        return [FakeMCP()]

    async def list_skills(self) -> list:
        return []


def _run_context(*, session_id: str, workspace: Any = None) -> RunContext:
    return RunContext(
        session_id=session_id,
        chat_id="chat-review",
        workspace=workspace if workspace is not None else FakeWorkspace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=TEST_TENANT_ID,
            workspace_id=TEST_WORKSPACE_ID,
            user_id=TEST_USER_ID,
            agent_id=ReviewAgentBuilder.AGENT_ID,
            session_id=session_id,
        ),
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=TEST_IDENTITY,
        session_factory=get_session_factory(),
    )


def test_get_agent_builder_selects_by_agent_id() -> None:
    assert isinstance(get_agent_builder("default"), DataAgentBuilder)
    assert isinstance(get_agent_builder("agent-a"), DataAgentBuilder)
    assert isinstance(
        get_agent_builder(ReviewAgentBuilder.AGENT_ID), ReviewAgentBuilder
    )


def test_session_review_links_parent_and_uses_reserved_id() -> None:
    parent = Session.create(identity=TEST_IDENTITY, title="月报")
    reviewed = Session.create(
        identity=TEST_IDENTITY,
        agent_id=ReviewAgentBuilder.AGENT_ID,
        title="审核：月报",
        parent_session_id=parent.id,
    )
    assert reviewed.agent_id == ReviewAgentBuilder.AGENT_ID
    assert reviewed.title == "审核：月报"
    assert reviewed.parent_session_id == parent.id
    assert reviewed.forked_from_chat_id is None
    assert reviewed.datasource_id is None
    assert reviewed.chat_count == 0


def test_review_workspace_seeds_review_skill_only(tmp_path: Path) -> None:
    paths = UserAgentPaths.from_identity(
        home=tmp_path / "home",
        identity=TEST_IDENTITY,
        agent_id=ReviewAgentBuilder.AGENT_ID,
        session_id="ses-review",
    )
    prepare_workspace_dirs(paths)
    names = [item.name for item in SkillStoreRepo(paths.skills).list_skills()]
    assert names == ["review-audit", "review-requirement"]
    assert all(item.enabled for item in SkillStoreRepo(paths.skills).list_skills())


@pytest.mark.asyncio
async def test_review_toolkit_has_review_tools_and_excludes_shell_mcp() -> None:
    toolkit = await ReviewAgentBuilder().build_toolkit(
        run_context=_run_context(session_id="ses-r")
    )
    tool_names = {
        schema["function"]["name"] for schema in await toolkit.get_tool_schemas()
    }
    assert PLAN_TOOL_NAMES <= tool_names
    assert {
        "get_overview",
        "get_plan",
        "get_trace",
        "get_artifacts",
        "read_artifact",
        "review_requirement",
        "Read",
        "Write",
    } <= tool_names
    assert "Bash" not in tool_names
    assert "PowerShell" not in tool_names
    assert "CronJob" not in tool_names
    assert "trigger_review_agent" not in tool_names
    assert "mcp__fake-mcp__execute_sql" not in tool_names
