"""Workspace 工厂与 CLI workspace_type 解析的回归测试。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolChunk

from datapaw.host.core.paths import Paths
from datapaw.host.core.utils.workspace import (
    ManagedDockerBash,
    _container_mcp_specs,
    _rewrite_loopback_url,
    create_docker_workspace,
    create_local_workspace,
)


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


async def _collect_chunks(chunks):
    return [chunk async for chunk in chunks]


@pytest.fixture()
def paths(tmp_path):
    return Paths(tmp_path, "sess-test")


def test_docker_workspace_mounts_skills_and_analysis_stack(paths, monkeypatch):
    monkeypatch.delenv("DATAPAW_DOCKER_BASE_IMAGE", raising=False)
    monkeypatch.delenv("DATAPAW_DOCKER_EXTRA_PIP", raising=False)
    ws = create_docker_workspace(paths)
    # DockerWorkspace.__init__ 不连接 daemon，纯构造可断言
    assert ws.base_image == "python:3.11-slim"
    assert "pandas" in ws.extra_pip
    assert "matplotlib" in ws.extra_pip
    # 技能目录与 local 工厂一致，非空
    assert ws.skill_paths, "skill_paths should be discovered, not empty"
    local = create_local_workspace(paths)
    assert list(ws.skill_paths) == list(local.skill_paths)


async def test_local_workspace_uses_native_shell(paths):
    from agentscope.tool import PowerShell

    tools = await create_local_workspace(paths).list_tools()

    if os.name == "nt":
        assert isinstance(tools[0], PowerShell)
    else:
        from datapaw.host.core.tools.workspace import WorkspaceBash

        assert isinstance(tools[0], WorkspaceBash)


@pytest.mark.skipif(os.name != "nt", reason="native Windows-only regression")
async def test_native_windows_shell_enforces_timeout(paths):
    tools = await create_local_workspace(paths).list_tools()
    shell = tools[0]

    stream = shell(command="Start-Sleep -Seconds 30", timeout=500)
    if inspect.iscoroutine(stream):
        # AgentScope's PowerShell tool is a coroutine yielding the stream.
        stream = await stream
    chunks = [chunk async for chunk in stream]

    assert chunks[-1].state == ToolResultState.ERROR
    assert "timed out" in chunks[-1].content[0].text.lower()


def test_docker_workspace_env_overrides(paths, monkeypatch):
    monkeypatch.setenv("DATAPAW_DOCKER_BASE_IMAGE", "python:3.12-slim")
    monkeypatch.setenv("DATAPAW_DOCKER_EXTRA_PIP", "scikit-learn, pandas ,")
    ws = create_docker_workspace(paths)
    assert ws.base_image == "python:3.12-slim"
    assert "scikit-learn" in ws.extra_pip
    # 去重：内置栈已有 pandas，不重复追加
    assert ws.extra_pip.count("pandas") == 1


async def test_managed_docker_bash_reaps_timed_out_process_group():
    delegate = RecordingBash()
    bash = ManagedDockerBash(delegate, grace_seconds=0.01)

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
    assert reaper_argv[-2].startswith("/tmp/datapaw-exec-")


async def test_managed_docker_bash_reaps_cancelled_process_group():
    delegate = RecordingBash(cancel_first=True)
    bash = ManagedDockerBash(delegate, grace_seconds=0.01)
    task = asyncio.create_task(
        _collect_chunks(bash.call(command="sleep 60")),
    )
    await delegate.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(delegate.calls) == 2
    reaper_argv = shlex.split(delegate.calls[1][0])
    assert reaper_argv[-2].startswith("/tmp/datapaw-exec-")


async def test_docker_workspace_wraps_bash_via_public_list_tools(
    paths,
    monkeypatch,
):
    from agentscope.workspace import DockerWorkspace

    async def fake_list_tools(self):
        return [Bash()]

    monkeypatch.setattr(DockerWorkspace, "list_tools", fake_list_tools)
    workspace = create_docker_workspace(paths)

    tools = await workspace.list_tools()

    assert len(tools) == 1
    assert isinstance(tools[0], ManagedDockerBash)


def test_resolve_workspace_type_precedence(monkeypatch):
    from datapaw.cli.util import resolve_workspace_type

    monkeypatch.delenv("DATAPAW_WORKSPACE", raising=False)
    assert resolve_workspace_type() == "docker"

    monkeypatch.setenv("DATAPAW_WORKSPACE", "docker")
    assert resolve_workspace_type() == "docker"

    # CLI 参数优先于环境变量
    args = argparse.Namespace(workspace="local")
    assert resolve_workspace_type(args) == "local"

    # 无 workspace 属性的 Namespace 回落到环境变量
    assert resolve_workspace_type(argparse.Namespace()) == "docker"


def test_resolve_workspace_type_invalid(monkeypatch):
    from datapaw.cli.util import resolve_workspace_type

    monkeypatch.setenv("DATAPAW_WORKSPACE", "e2b")
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
def test_rewrite_loopback_url(url, expected):
    assert _rewrite_loopback_url(url, "host.docker.internal") == expected


async def test_docker_initialize_uses_public_hook_and_restores_host_mcp(
    paths,
    monkeypatch,
):
    from agentscope.workspace import DockerWorkspace

    mcp_path = paths.mcp_config_file
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    host_specs = [
        {
            "name": "databridge",
            "is_stateful": False,
            "mcp_config": {
                "type": "http_mcp",
                "url": "http://127.0.0.1:8765/mcp/v1/cm",
                "headers": {},
                "timeout": 10,
            },
        },
    ]
    mcp_path.write_text(json.dumps(host_specs), encoding="utf-8")
    observed: list[dict] = []
    monkeypatch.setenv("DATAPAW_CLIENT_API_TOKEN", "runtime-client-secret")
    monkeypatch.setenv("DATAPAW_API_TOKEN", "fallback-secret")

    async def fake_initialize(self):
        observed.extend(json.loads(mcp_path.read_text(encoding="utf-8")))

    monkeypatch.setattr(DockerWorkspace, "initialize", fake_initialize)
    workspace = create_docker_workspace(paths)

    assert "_restore_or_seed_mcps" not in type(workspace).__dict__
    await workspace.initialize()

    assert observed[0]["mcp_config"]["url"].startswith(
        "http://host.docker.internal:8765/"
    )
    assert observed[0]["mcp_config"]["headers"] == {
        "Authorization": "Bearer runtime-client-secret",
    }
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == host_specs


def test_container_mcp_specs_does_not_leak_token_to_remote_endpoint(
    monkeypatch,
):
    monkeypatch.setenv("DATAPAW_CLIENT_API_TOKEN", "do-not-leak")
    remote_specs = [
        {
            "name": "remote",
            "mcp_config": {
                "type": "http_mcp",
                "url": "https://mcp.example.com/mcp/v1/cm",
                "headers": {"X-Existing": "preserved"},
            },
        },
    ]

    prepared = _container_mcp_specs(remote_specs, "host.docker.internal")

    assert prepared[0]["mcp_config"]["headers"] == {
        "X-Existing": "preserved",
    }
    assert remote_specs[0]["mcp_config"]["headers"] == {
        "X-Existing": "preserved",
    }
