"""Service workspace ownership and runtime-visible artifact paths."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw_data.host.core.agent.middleware.prompt import QwenPawDataPromptMiddleware
from qwenpaw_data.host.core.agent.middleware import sql_artifact
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.workspace import (
    QwenPawDataWorkspaceManager,
    stable_workspace_id,
)

from .conftest import TEST_IDENTITY


def _paths(tmp_path):
    return UserAgentPaths.from_identity(
        home=tmp_path,
        identity=TEST_IDENTITY,
        agent_id="default",
        session_id="session-a",
    )


@pytest.mark.parametrize("field", ["tenant_id", "workspace_id", "user_id", "agent_id"])
async def test_sessions_share_workspace_only_inside_the_same_identity_scope(
    tmp_path, field
):
    first = _paths(tmp_path)
    second = replace(first, session_id="session-b")
    other = replace(first, **{field: "other"})
    manager = QwenPawDataWorkspaceManager()
    try:
        async with manager.lease(first) as a:
            async with manager.lease(second) as b:
                async with manager.lease(other) as c:
                    assert a is b and c is not a
                    assert first.workspace == second.workspace != other.workspace
                    assert first.artifact_dir != second.artifact_dir
                    assert first.uploads_dir != second.uploads_dir
                    assert stable_workspace_id(first) != stable_workspace_id(other)
                    assert a.workdir == str(first.workspace)
                    assert c.workdir == str(other.workspace)
    finally:
        await manager.stop()
    assert not a.is_alive and not c.is_alive


@pytest.mark.parametrize("backend", ["local", "docker"])
async def test_prompt_and_sql_results_use_the_same_session_artifact_mapping(
    tmp_path,
    monkeypatch,
    backend,
):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    clear_settings()
    paths = _paths(tmp_path)
    workdir = "/workspace" if backend == "docker" else paths.workspace.as_posix()
    context = RunContext(
        session_id=paths.session_id,
        chat_id="chat-a",
        paths=paths,
        workspace=SimpleNamespace(workdir=workdir),
        identity=TEST_IDENTITY,
        session_factory=None,
        user_runtime_config=SimpleNamespace(),
        request_context={"access_token": "test-cm-token"},
    )
    expected_dir = f"{workdir}/artifacts/session-a"
    prompt = await QwenPawDataPromptMiddleware(run_context=context).on_system_prompt(
        None,
        "existing skill instructions",
    )
    assert expected_dir in prompt and workdir in prompt
    assert "existing skill instructions" in prompt
    assert backend in prompt

    original = '{"result_id":"test-result"}'
    materialize = AsyncMock(
        return_value=json.dumps(
            {
                "file_path": str(paths.artifact_dir / "result.csv"),
                "row_count": 1,
            }
        )
    )
    monkeypatch.setattr(sql_artifact, "materialize_execute_sql_result", materialize)
    middleware = sql_artifact.SqlArtifactMiddleware(run_context=context)

    async def next_handler(**kwargs):
        yield ToolResponse(
            content=[TextBlock(text=original)], state=ToolResultState.SUCCESS
        )

    results = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(_request_context=context.request_context),
            {"tool_call": SimpleNamespace(name="mcp__context-manager__execute_read")},
            next_handler,
        )
    ]
    materialize.assert_awaited_once_with(
        original,
        artifact_dir=paths.artifact_dir,
        access_token="test-cm-token",
    )
    assert results[0].state == ToolResultState.SUCCESS
    assert json.loads(results[0].content[0].text) == {
        "file_path": expected_dir + "/result.csv",
        "row_count": 1,
    }
    if backend == "docker":
        assert str(tmp_path) not in prompt
        assert str(tmp_path) not in results[0].content[0].text
