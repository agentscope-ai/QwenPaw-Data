from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from datapaw.host.core import core as core_module
from datapaw.host.core.core import DATAPAW_AGENT_NAME, DataPawHost
from datapaw.host.core.registry import DataPawHostRegistry


class FakeWorkspace:
    async def initialize(self) -> None:
        return None


class FakeAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_registry_keys_hosts_only_by_session(tmp_path: Path) -> None:
    registry = DataPawHostRegistry(
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
        DataPawHost(  # type: ignore[call-arg]
            home=tmp_path,
            model=object(),
            agent_id="legacy",
        )

    registry = DataPawHostRegistry(home=tmp_path, model=object())
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
async def test_top_level_agent_uses_stable_datapaw_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_toolkit(*args: Any, **kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(core_module, "build_datapaw_toolkit", fake_build_toolkit)
    monkeypatch.setattr(core_module, "DataPawAgent", FakeAgent)

    host = DataPawHost(
        home=tmp_path,
        model=object(),
        workspace=FakeWorkspace(),
        session_id="session-a",
    )

    agent = await host._get_agent(mode="agent")

    assert DATAPAW_AGENT_NAME == "datapaw"
    assert agent.kwargs["name"] == DATAPAW_AGENT_NAME
    assert agent.kwargs["session_id"] == "session-a"
