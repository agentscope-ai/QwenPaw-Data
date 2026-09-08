from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qwenpaw_data.host.core import core as core_module
from qwenpaw_data.host.core.core import QWENPAW_DATA_AGENT_NAME, QwenPawDataHost
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry


class FakeWorkspace:
    def __init__(self, workdir: str | None = None) -> None:
        if workdir is not None:
            self.workdir = workdir

    async def initialize(self) -> None:
        return None


class FakeAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_registry_keys_hosts_only_by_session(tmp_path: Path) -> None:
    registry = QwenPawDataHostRegistry(
        home=tmp_path,
        model=object(),
        workspace=FakeWorkspace(),
    )

    first = registry.get(session_id="session-a")
    same = registry.get(session_id="session-a")
    other = registry.get(session_id="session-b")

    assert first is same
    assert other is not first
    assert first.paths.workspace == tmp_path / "host" / "workspace"
    assert other.paths.workspace == first.paths.workspace
    assert first.paths.artifact_dir != other.paths.artifact_dir
    assert first.session_store.get_path("session-a") == (
        first.paths.console_root / "default_session-a.json"
    )
    assert other.session_store.get_path("session-b") == (
        other.paths.console_root / "default_session-b.json"
    )
    assert first.session_store.get_path("session-a") != (
        other.session_store.get_path("session-b")
    )
    assert first.dag_store.dag_path("session-a") == (
        first.paths.dag_root / "default_session-a.json"
    )
    assert other.dag_store.dag_path("session-b") == (
        other.paths.dag_root / "default_session-b.json"
    )
    assert first.dag_store.dag_path("session-a") != (
        other.dag_store.dag_path("session-b")
    )
    assert not registry.is_running(session_id="session-a")


def test_agent_id_is_not_accepted_by_host_or_registry(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="agent_id"):
        QwenPawDataHost(  # type: ignore[call-arg]
            home=tmp_path,
            model=object(),
            agent_id="legacy",
        )

    registry = QwenPawDataHostRegistry(home=tmp_path, model=object())
    with pytest.raises(TypeError, match="agent_id"):
        registry.get(  # type: ignore[call-arg]
            agent_id="legacy",
            session_id="session-a",
        )
    with pytest.raises(TypeError, match="agent_id"):
        registry.is_running(  # type: ignore[call-arg]
            agent_id="legacy",
            session_id="session-a",
        )


@pytest.mark.asyncio
async def test_top_level_agent_uses_runtime_visible_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolkit_kwargs: dict[str, Any] = {}

    async def fake_build_toolkit(*args: Any, **kwargs: Any) -> object:
        toolkit_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(core_module, "build_qwenpaw_data_toolkit", fake_build_toolkit)
    monkeypatch.setattr(core_module, "QwenPawDataAgent", FakeAgent)

    host = QwenPawDataHost(
        home=tmp_path,
        model=object(),
        workspace=FakeWorkspace("/workspace"),
        session_id="session-a",
    )

    agent = await host._get_agent(mode="agent")

    assert QWENPAW_DATA_AGENT_NAME == "qwenpaw-data"
    assert agent.kwargs["name"] == QWENPAW_DATA_AGENT_NAME
    assert agent.kwargs["session_id"] == "session-a"
    assert agent.kwargs["workspace_dir"] == Path("/workspace")
    assert agent.kwargs["artifact_dir"] == Path(
        "/workspace/artifacts/session-a",
    )
    sql_middleware = agent.kwargs["middlewares"][0]
    assert sql_middleware._host_artifact_dir == host.paths.artifact_dir
    assert sql_middleware._model_artifact_dir == Path(
        "/workspace/artifacts/session-a",
    )
    assert toolkit_kwargs["workspace_dir"] == Path("/workspace")
    assert toolkit_kwargs["artifacts_root"] == Path("/workspace/artifacts")
    assert toolkit_kwargs["host_artifact_dir"] == host.paths.artifact_dir
    artifact_context = agent.kwargs["runtime_state"]._artifact_path_context
    assert artifact_context.host_artifact_dir == host.paths.artifact_dir.resolve()
    assert artifact_context.model_artifact_dir == "/workspace/artifacts/session-a"


@pytest.mark.asyncio
async def test_model_paths_fall_back_without_workspace_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolkit_kwargs: dict[str, Any] = {}

    async def fake_build_toolkit(*args: Any, **kwargs: Any) -> object:
        toolkit_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(core_module, "build_qwenpaw_data_toolkit", fake_build_toolkit)
    monkeypatch.setattr(core_module, "QwenPawDataAgent", FakeAgent)

    host = QwenPawDataHost(
        home=tmp_path,
        model=object(),
        workspace=FakeWorkspace(),
        session_id="session-a",
    )

    agent = await host._get_agent(mode="agent")

    assert agent.kwargs["workspace_dir"] == host.paths.workspace
    assert agent.kwargs["artifact_dir"] == host.paths.artifact_dir
    assert toolkit_kwargs["workspace_dir"] == host.paths.workspace
    assert toolkit_kwargs["artifacts_root"] == host.paths.artifacts_root
    assert toolkit_kwargs["host_artifact_dir"] == host.paths.artifact_dir
    artifact_context = agent.kwargs["runtime_state"]._artifact_path_context
    assert artifact_context.model_artifact_dir == str(host.paths.artifact_dir)
