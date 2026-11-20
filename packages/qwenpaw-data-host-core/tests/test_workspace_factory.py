"""Service workspace assembly, protected Docker tools and MCP projection."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolChunk
from agentscope.workspace import LocalWorkspace

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.domain.mcp import Mcp
from qwenpaw_data.host.core.integrations.cm.mcp import (
    project_workspace_mcp_client,
    rewrite_loopback_url_for_docker,
)
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.workspace import (
    QwenPawDataWorkspaceManager,
    stable_workspace_id,
)
from qwenpaw_data.host.core.workspace.docker_workspace import (
    ManagedDockerBash,
    QwenPawDataDockerWorkspace,
)
from . import test_mcp_gateway_sync

mcp_http = test_mcp_gateway_sync.mcp_http


class RecordingBash:
    def __init__(self, *, cancel_first: bool = False) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.cancel_first = cancel_first
        self.started = asyncio.Event()

    async def call(self, command, description="", timeout=120000):
        self.calls.append((command, description, timeout))
        if len(self.calls) == 1:
            if self.cancel_first:
                self.started.set()
                await asyncio.Event().wait()
            yield ToolChunk(
                content=[TextBlock(text="Command timed out after 100ms")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        yield ToolChunk(
            content=[TextBlock(text="")],
            state=ToolResultState.RUNNING,
            is_last=True,
        )


class ResultBash:
    def __init__(self, text: str, state=ToolResultState.RUNNING) -> None:
        self.text = text
        self.state = state

    async def call(self, command, description="", timeout=120000):
        _ = (command, description, timeout)
        yield ToolChunk(
            content=[TextBlock(text=self.text)],
            state=self.state,
            is_last=True,
            metadata={"source": "delegate"},
        )


async def _collect_chunks(chunks):
    return [chunk async for chunk in chunks]


def _managed(delegate, **kwargs):
    return ManagedDockerBash(delegate, cleanup_failure_handler=AsyncMock(), **kwargs)


@pytest.fixture
def paths(tmp_path):
    return UserAgentPaths(tmp_path, "tenant", "workspace", "user", "default", "session")


@pytest.fixture
async def workspace_factory(paths, monkeypatch):
    # Exercise the real manager construction and skill seeding, not the daemon.
    monkeypatch.setattr(QwenPawDataDockerWorkspace, "initialize", AsyncMock())
    manager = QwenPawDataWorkspaceManager()
    created = []

    async def make(backend):
        monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
        clear_settings()
        workspace = await manager._create_workspace(paths, stable_workspace_id(paths))
        created.append(workspace)
        return workspace

    try:
        yield make
    finally:
        for workspace in created:
            await workspace.close()


async def test_docker_workspace_seeds_skills_and_analysis_stack(
    paths, workspace_factory, monkeypatch
):
    for key in (
        "QWENPAW_DATA_DOCKER_BASE_IMAGE",
        "QWENPAW_DATA_DOCKER_EXTRA_PIP",
        "QWENPAW_DATA_WORKSPACE_DOCKER_BASE_IMAGE",
        "QWENPAW_DATA_WORKSPACE_DOCKER_EXTRA_PIP",
    ):
        monkeypatch.delenv(key, raising=False)
    docker = await workspace_factory("docker")
    assert docker.base_image == "python:3.11-slim"
    assert {"pandas", "matplotlib"} <= set(docker.extra_pip)
    assert (paths.skills / "skill-hub-guide" / "SKILL.md").is_file()
    local = await workspace_factory("local")
    skills = await local.list_skills(agent_id=paths.agent_id)
    assert "skill-hub-guide" in {skill.name for skill in skills}
    assert all(str(paths.skills) in str(skill.dir) for skill in skills)


async def test_local_workspace_uses_native_shell(workspace_factory):
    from agentscope.tool import PowerShell

    tools = await (await workspace_factory("local")).list_tools()
    assert isinstance(tools[0], PowerShell if os.name == "nt" else Bash)


@pytest.mark.skipif(os.name != "nt", reason="native Windows-only regression")
async def test_native_windows_shell_enforces_timeout(workspace_factory):
    tools = await (await workspace_factory("local")).list_tools()
    stream = tools[0](command="Start-Sleep -Seconds 30", timeout=500)
    if inspect.iscoroutine(stream):
        stream = await stream
    chunks = [chunk async for chunk in stream]
    assert chunks[-1].state == ToolResultState.ERROR
    assert "timed out" in chunks[-1].content[0].text.lower()


async def test_docker_workspace_env_overrides(workspace_factory, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_DOCKER_BASE_IMAGE", "python:3.12-slim")
    monkeypatch.setenv(
        "QWENPAW_DATA_WORKSPACE_DOCKER_EXTRA_PIP", "scikit-learn, pandas ,"
    )
    workspace = await workspace_factory("docker")
    assert workspace.base_image == "python:3.12-slim"
    assert "scikit-learn" in workspace.extra_pip
    assert workspace.extra_pip.count("pandas") == 1


async def test_managed_docker_bash_reaps_timed_out_process_group():
    delegate = RecordingBash()
    bash = _managed(delegate, grace_seconds=0.01)

    chunks = await _collect_chunks(
        bash.call(command="sleep 60", timeout=100),
    )

    assert chunks[-1].state == ToolResultState.ERROR
    assert "Command timed out after 100ms: sleep 60" in chunks[-1].content[0].text
    assert len(delegate.calls) == 2
    supervised, _, timeout = delegate.calls[0]
    supervised_argv = shlex.split(supervised)
    assert supervised_argv[:2] == ["python", "-c"]
    assert supervised_argv[-3:] == ["/bin/sh", "-c", "sleep 60"]
    assert timeout == 100
    reaper_argv = shlex.split(delegate.calls[1][0])
    assert reaper_argv[:2] == ["python", "-c"]
    assert reaper_argv[-1] == "0.01"
    assert reaper_argv[-2].startswith("/tmp/qwenpaw-data-exec-")


async def test_managed_docker_bash_reaps_cancelled_process_group():
    delegate = RecordingBash(cancel_first=True)
    bash = _managed(delegate, grace_seconds=0.01)
    task = asyncio.create_task(
        _collect_chunks(bash.call(command="sleep 60")),
    )
    await delegate.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(delegate.calls) == 2
    reaper_argv = shlex.split(delegate.calls[1][0])
    assert reaper_argv[-2].startswith("/tmp/qwenpaw-data-exec-")


async def test_managed_docker_bash_reports_silent_success() -> None:
    bash = _managed(ResultBash(""))

    chunks = await _collect_chunks(bash.call(command="true"))

    assert chunks[-1].state == ToolResultState.RUNNING
    assert chunks[-1].content[0].text == (
        "Command completed successfully with no stdout or stderr."
    )
    assert chunks[-1].metadata == {"source": "delegate"}


async def test_managed_docker_bash_preserves_nonempty_output() -> None:
    bash = _managed(ResultBash("created report.html"))

    chunks = await _collect_chunks(bash.call(command="python report.py"))

    assert chunks[-1].content[0].text == "created report.html"
    assert chunks[-1].metadata == {"source": "delegate"}


async def test_managed_docker_bash_preserves_errors() -> None:
    bash = _managed(ResultBash("failed", ToolResultState.ERROR))

    chunks = await _collect_chunks(bash.call(command="false"))

    assert chunks[-1].state == ToolResultState.ERROR
    assert chunks[-1].content[0].text == "failed"


async def test_docker_workspace_wraps_bash_via_public_list_tools(
    workspace_factory, monkeypatch
):
    from agentscope.workspace import DockerWorkspace

    async def fake_list_tools(self):
        return [Bash()]

    monkeypatch.setattr(DockerWorkspace, "list_tools", fake_list_tools)
    workspace = await workspace_factory("docker")
    tools = await workspace.list_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], ManagedDockerBash)
    assert tools[0]._cleanup_failure_handler == workspace.close


def test_resolve_workspace_type_precedence(monkeypatch):
    from qwenpaw_data.cli.util import resolve_workspace_type

    monkeypatch.delenv("QWENPAW_DATA_WORKSPACE", raising=False)
    assert resolve_workspace_type() == "docker"

    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE", "docker")
    assert resolve_workspace_type() == "docker"

    # CLI 参数优先于环境变量
    args = argparse.Namespace(workspace="local")
    assert resolve_workspace_type(args) == "local"

    # 无 workspace 属性的 Namespace 回落到环境变量
    assert resolve_workspace_type(argparse.Namespace()) == "docker"


def test_resolve_workspace_type_invalid(monkeypatch):
    from qwenpaw_data.cli.util import resolve_workspace_type

    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE", "e2b")
    with pytest.raises(ValueError, match="invalid workspace type"):
        resolve_workspace_type()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://127.0.0.1:8765/mcp/v1/cm",
            "http://host.docker.internal:8765/mcp/v1/cm",
        ),
        (
            "http://localhost:8765/mcp/v1/cm?x=1",
            "http://host.docker.internal:8765/mcp/v1/cm?x=1",
        ),
        ("http://127.0.0.1/health", "http://host.docker.internal/health"),
        # 非 loopback 不改写
        ("https://api.example.com/mcp", "https://api.example.com/mcp"),
        ("http://192.168.1.5:8765/mcp", "http://192.168.1.5:8765/mcp"),
    ],
)
def test_rewrite_loopback_url_for_docker(url, expected):
    assert rewrite_loopback_url_for_docker(url, "host.docker.internal") == expected


def test_docker_projection_keeps_host_inventory_unchanged(paths, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "docker")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("QWENPAW_DATA_CLIENT_API_TOKEN", "runtime-client-secret")
    clear_settings()
    repo = McpRepository(paths.mcp_manifest)
    source = repo.require("context-manager").to_client()
    before = paths.mcp_manifest.read_bytes()
    projected = project_workspace_mcp_client(source)
    assert projected.mcp_config.url == "http://host.docker.internal:8765/mcp/v1/cm"
    assert source.mcp_config.url == "http://127.0.0.1:8765/mcp/v1/cm"
    assert projected.mcp_config.headers == source.mcp_config.headers
    assert paths.mcp_manifest.read_bytes() == before
    assert b"runtime-client-secret" not in before


async def test_mcp_sync_does_not_leak_cm_token_to_remote_endpoint(paths, mcp_http):
    repo = McpRepository(paths.mcp_manifest)
    repo.create(
        Mcp.from_record(
            {
                "name": "remote",
                "source": "custom",
                "enabled": True,
                "is_stateful": False,
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "https://mcp.example.com/mcp/v1/cm",
                    "headers": {"X-Existing": "preserved"},
                },
            }
        )
    )
    workspace = LocalWorkspace(workdir=str(paths.workspace))
    scope = {"agent_id": paths.agent_id, "session_id": paths.session_id}
    try:
        await workspace.initialize()
        await repo.sync(
            workspace,
            **scope,
            request_id="chat",
            mcp_headers={},
            access_token="do-not-leak",
        )
        clients = {client.name: client for client in await workspace.list_mcps(**scope)}
        remote = clients["remote"]
        assert remote.mcp_config.headers == {"X-Existing": "preserved"}
        assert "do-not-leak" not in str(remote.model_dump(mode="json"))
        assert "do-not-leak" not in str(remote._runtime_headers)
        assert (
            clients["context-manager"]._runtime_headers["Authorization"]
            == "Bearer do-not-leak"
        )
        assert "do-not-leak" not in paths.mcp_manifest.read_text()
        assert "do-not-leak" not in (paths.workspace / ".mcp").read_text()
    finally:
        await workspace.close()


async def test_mcp_sync_scopes_changes_to_the_target_session(paths, mcp_http):
    repo = McpRepository(paths.mcp_manifest)
    workspace = LocalWorkspace(workdir=str(paths.workspace))
    first = {"agent_id": paths.agent_id, "session_id": "first"}
    second = {"agent_id": paths.agent_id, "session_id": "second"}
    try:
        await workspace.initialize()
        await repo.sync(workspace, **first, request_id="chat-1", mcp_headers={})
        repo.disable("context-manager")
        await repo.sync(workspace, **second, request_id="chat-2", mcp_headers={})
        assert await workspace.list_mcps(**second) == []
        assert [c.name for c in await workspace.list_mcps(**first)] == [
            "context-manager"
        ]
        await repo.sync(workspace, **first, request_id="chat-3", mcp_headers={})
        assert await workspace.list_mcps(**first) == []
    finally:
        await workspace.close()
