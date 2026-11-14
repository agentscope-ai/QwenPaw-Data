from __future__ import annotations

import pytest
from agentscope.mcp import MCPClient
from agentscope.workspace import LocalWorkspace

from qwenpaw_data.host.core.integrations.cm import (
    ensure_workspace_cm_mcp,
    is_cm_mcp_client,
    project_workspace_mcp_client,
)
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.utils.mcp import load_builtin_mcp_records

_CM_MCP_URL = "http://localhost:8765/mcp/v1/cm"


def _mcp_client(
    *,
    name: str = "context-manager",
    url: str,
    timeout: float | None = 30.0,
    execution_timeout: float | None = None,
) -> MCPClient:
    return MCPClient(
        name=name,
        is_stateful=False,
        mcp_config={
            "type": "http_mcp",
            "url": url,
            "headers": {},
            "timeout": timeout,
        },
        enable_tools=None,
        disable_tools=None,
        execution_timeout=execution_timeout,
    )


def test_is_cm_mcp_client_by_name() -> None:
    assert is_cm_mcp_client(_mcp_client(url="http://example.com/mcp")) is True


def test_is_cm_mcp_client_rejects_other_name() -> None:
    assert is_cm_mcp_client(_mcp_client(name="search", url=_CM_MCP_URL)) is False


def test_builtin_mcp_preset_is_context_manager() -> None:
    records = load_builtin_mcp_records()
    assert "context-manager" in records
    record = records["context-manager"]
    assert record["source"] == "builtin"
    assert record["mcp_config"]["url"] == ""
    assert record["mcp_config"]["timeout"] == 2400.0
    assert record["disable_tools"] == [
        "execute_create",
        "execute_insert",
        "execute_update",
        "execute_modify",
    ]


def test_project_workspace_mcp_rewrites_loopback_for_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "docker")
    clear_settings()
    try:
        client = _mcp_client(url="http://127.0.0.1:8765/mcp/v1/cm")
        assert (
            project_workspace_mcp_client(client).mcp_config.url
            == "http://host.docker.internal:8765/mcp/v1/cm"
        )
    finally:
        monkeypatch.delenv("QWENPAW_DATA_WORKSPACE_BACKEND", raising=False)
        clear_settings()


@pytest.mark.asyncio
async def test_mcp_repo_sync_seeds_builtin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def list_raw_tools(self: MCPClient) -> list:
        return []

    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)
    repo = McpRepository(tmp_path / "ws" / "mcp.json")
    workspace = LocalWorkspace(workdir=str(tmp_path / "ws"))
    try:
        await workspace.initialize()
        assert await workspace.list_mcps() == []

        await repo.sync(
            workspace,
            agent_id="agent",
            session_id="session",
            request_id="chat_1",
            mcp_headers={},
        )
        mcp = repo.require("context-manager")
        assert mcp.source == "builtin"
        mcps = await workspace.list_mcps(agent_id="agent", session_id="session")
        assert len(mcps) == 1
        assert is_cm_mcp_client(mcps[0]) is True
    finally:
        await workspace.close()


@pytest.mark.asyncio
async def test_ensure_workspace_cm_mcp_does_not_create(tmp_path) -> None:
    workspace = LocalWorkspace(workdir=str(tmp_path / "ws"))
    try:
        await workspace.initialize()
        await ensure_workspace_cm_mcp(
            workspace,
            agent_id="agent-a",
            session_id="ses-1",
            chat_id="chat-1",
        )
        assert await workspace.list_mcps(agent_id="agent-a", session_id="ses-1") == []
    finally:
        await workspace.close()


@pytest.mark.asyncio
async def test_ensure_workspace_cm_mcp_retokens_a_restored_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace outlives the token it was first built with."""

    async def list_raw_tools(self: MCPClient) -> list:
        return []

    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)
    workdir = tmp_path / "ws"
    repo = McpRepository(workdir / "mcp.json")
    scope = {"agent_id": "agent-a", "session_id": "ses-1"}
    workspace = LocalWorkspace(workdir=str(workdir))
    try:
        await workspace.initialize()
        await repo.sync(workspace, request_id="chat-1", mcp_headers={}, **scope)
        await ensure_workspace_cm_mcp(
            workspace,
            access_token="expired-token",
            chat_id="chat-1",
            **scope,
        )
    finally:
        await workspace.close()

    # The credential must not survive in mcp.json or .mcp for the next process.
    assert "expired-token" not in (workdir / "mcp.json").read_text()
    assert "expired-token" not in (workdir / ".mcp").read_text()

    restored = LocalWorkspace(workdir=str(workdir))
    try:
        await restored.initialize()
        await ensure_workspace_cm_mcp(
            restored,
            access_token="fresh-token",
            chat_id="chat-1",
            **scope,
        )

        mcps = await restored.list_mcps(**scope)
        assert len(mcps) == 1
        assert "Authorization" not in (mcps[0].mcp_config.headers or {})
        assert mcps[0]._runtime_headers["Authorization"] == "Bearer fresh-token"
    finally:
        await restored.close()
