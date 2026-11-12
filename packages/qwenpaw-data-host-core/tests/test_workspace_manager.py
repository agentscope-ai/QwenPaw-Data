# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.tool import Bash, Read

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.settings.workspace import WorkspaceSettings
from qwenpaw_data.host.core.workspace import (
    QwenPawDataWorkspaceManager,
    stable_workspace_id,
)


def _paths(tmp_path: Path, *, session_id: str = "session-a") -> UserAgentPaths:
    return UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id="agent-a",
        session_id=session_id,
    )


def test_stable_workspace_id_ignores_session(tmp_path: Path) -> None:
    a = stable_workspace_id(_paths(tmp_path, session_id="s1"))
    b = stable_workspace_id(_paths(tmp_path, session_id="s2"))
    assert a == b
    assert len(a) == 16


@pytest.mark.asyncio
async def test_manager_reuses_local_workspace_within_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "local")
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_TTL", "3600")
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_SWEEP_INTERVAL", "3600")
    clear_settings()

    manager = QwenPawDataWorkspaceManager()
    await manager.start()
    try:
        paths = _paths(tmp_path / "home")
        async with manager.lease(paths) as first:
            assert first.is_alive
            skills = await first.list_skills(agent_id=paths.agent_id)
            assert skills
            assert all(Path(skill.dir).parent == paths.skills for skill in skills)
            tools = await first.list_tools()
            tool_types = {type(tool) for tool in tools}
            assert Bash in tool_types
            assert Read in tool_types
        async with manager.lease(
            _paths(tmp_path / "home", session_id="session-b")
        ) as second:
            assert first is second
        first.purge_session = AsyncMock()
        await manager.purge_session(paths, session_id=paths.session_id)
        first.purge_session.assert_awaited_once_with(
            agent_id=paths.agent_id,
            session_id=paths.session_id,
        )
        assert first.is_alive
    finally:
        await manager.stop()
        clear_settings()


@pytest.mark.asyncio
async def test_manager_rejects_invalid_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "kvm")
    clear_settings()
    with pytest.raises(ValueError, match="must be one of .*acs-sandbox"):
        WorkspaceSettings()
    monkeypatch.delenv("QWENPAW_DATA_WORKSPACE_BACKEND", raising=False)
    clear_settings()


@pytest.mark.asyncio
async def test_docker_warmup_initializes_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "docker")
    clear_settings()

    calls: list[str] = []

    class FakeDockerWorkspace:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.is_alive = False

        async def initialize(self) -> None:
            calls.append("initialize")
            self.is_alive = True

        async def close(self) -> None:
            calls.append("close")
            self.is_alive = False

    monkeypatch.setattr(
        "agentscope.workspace.DockerWorkspace",
        FakeDockerWorkspace,
    )

    manager = QwenPawDataWorkspaceManager()
    await manager.start()
    try:
        assert calls == ["initialize", "close"]
        await manager.start()
        assert calls == ["initialize", "close"]
    finally:
        await manager.stop()
        monkeypatch.delenv("QWENPAW_DATA_WORKSPACE_BACKEND", raising=False)
        clear_settings()


async def test_sweeper_keeps_renewing_other_workspaces(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test")
    clear_settings()
    manager = QwenPawDataWorkspaceManager()
    release, stalled, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ready = {name: asyncio.Event() for name in ("slow", "healthy")}

    async def slow_renew():
        stalled.set()
        await release.wait()

    workspaces = {
        "slow": SimpleNamespace(
            is_alive=True, renew=AsyncMock(side_effect=slow_renew), close=AsyncMock()
        ),
        "healthy": SimpleNamespace(
            is_alive=True, renew=AsyncMock(side_effect=renewed.set), close=AsyncMock()
        ),
    }
    manager._create_workspace = AsyncMock(
        side_effect=lambda paths, _wid: workspaces[paths.agent_id]
    )

    async def hold(name):
        async with manager.lease(replace(_paths(tmp_path), agent_id=name)):
            ready[name].set()
            await asyncio.Event().wait()

    holders = [asyncio.create_task(hold(name)) for name in workspaces]
    try:
        for event in ready.values():
            await asyncio.wait_for(event.wait(), 2)
        manager._schedule_sweep()
        await asyncio.wait_for(stalled.wait(), 2)
        await asyncio.wait_for(renewed.wait(), 2)
        assert workspaces["healthy"].renew.await_count == 1

        # The next tick must also proceed while the first workspace is still blocked.
        renewed.clear()
        manager._schedule_sweep()
        await asyncio.wait_for(renewed.wait(), 2)
        assert workspaces["healthy"].renew.await_count == 2
        assert workspaces["slow"].renew.await_count == 1
    finally:
        release.set()
        for holder in holders:
            holder.cancel()
        await asyncio.gather(*holders, return_exceptions=True)
        await manager.stop()
    assert not manager._sweep_jobs
    assert not manager._cache


@pytest.mark.parametrize("backend", ["local", "docker", "fc-sandbox", "acs-sandbox"])
async def test_invalid_sandbox_settings_only_block_cloud(
    backend, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from qwenpaw_data.host.core.workspace import workspace_manager

    for name, value in {
        "QWENPAW_DATA_WORKSPACE_BACKEND": backend,
        "QWENPAW_DATA_CM_SANDBOX_BASE_URL": "https://gateway.example.test",
        "QWENPAW_DATA_WORKSPACE_SANDBOX_TIMEOUT_SECONDS": "0",
        "FC_API_KEY": "test-key",
        "FC_API_URL": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        "FC_DOMAIN": "cn-hangzhou.e2b.fc.aliyuncs.com",
        "FC_TEMPLATE_ID": "test-template",
        "FC_ROLE_ARN_PATTERN": "role/{uid}",
        "QWENPAW_DATA_WORKSPACE_OSS_BUCKET": "test-bucket",
        "QWENPAW_DATA_WORKSPACE_OSS_PREFIX": "data",
        "QWENPAW_DATA_WORKSPACE_OSS_ENDPOINT": "https://oss.example.com",
        "ACS_API_KEY": "e2b_0123abcd",
        "ACS_DOMAIN": "sandbox.example.com",
        "ACS_TEMPLATE": "test-template",
        "ACS_OSS_PV": "test-pv",
        "ACS_CREDENTIAL_PROVIDER": "test-provider",
        "ACS_AGENT_NAME": "test-agent",
    }.items():
        monkeypatch.setenv(name, value)
    clear_settings()
    workspace = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(workspace_manager, "LocalWorkspace", lambda **kwargs: workspace)
    monkeypatch.setattr(
        "agentscope.workspace.DockerWorkspace", lambda **kwargs: workspace
    )
    prepare = Mock()
    monkeypatch.setattr(workspace_manager, "prepare_workspace_dirs", prepare)
    manager = QwenPawDataWorkspaceManager()
    paths = _paths(tmp_path)
    if backend in {"fc-sandbox", "acs-sandbox"}:
        with pytest.raises(ValueError, match="timeout_seconds"):
            await manager._create_workspace(paths, "test-workspace")
        prepare.assert_not_called()
    else:
        result = await manager._create_workspace(paths, "test-workspace")
        assert result is workspace
        prepare.assert_called_once_with(paths)
        workspace.initialize.assert_awaited_once()
