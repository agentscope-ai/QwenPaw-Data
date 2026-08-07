from __future__ import annotations

from pathlib import Path

import pytest

from datapaw.host.core.paths import (
    Paths,
    host_root,
    resolve_datapaw_home,
)


def test_resolve_datapaw_home_prefers_explicit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPAW_HOME", str(tmp_path / "from-env"))

    assert resolve_datapaw_home(tmp_path / "explicit") == (
        tmp_path / "explicit"
    ).resolve()


def test_resolve_datapaw_home_uses_trimmed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "from-env"
    monkeypatch.setenv("DATAPAW_HOME", f"  {expected}  ")

    assert resolve_datapaw_home() == expected.resolve()


def test_resolve_datapaw_home_uses_default_for_blank_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_user_home = tmp_path / "user"
    expected = fake_user_home / ".datapaw"
    monkeypatch.setenv("DATAPAW_HOME", "   ")
    monkeypatch.setenv("HOME", str(fake_user_home))

    assert resolve_datapaw_home() == expected.resolve()


def test_resolve_datapaw_home_expands_user_without_creating_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_user_home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(fake_user_home))

    resolved = resolve_datapaw_home("~/custom-datapaw")

    assert resolved == (fake_user_home / "custom-datapaw").resolve()
    assert not resolved.exists()


def test_paths_cover_host_state_contract_without_creating_it(
    tmp_path: Path,
) -> None:
    home = tmp_path / "state-home"
    paths = Paths(home, session_id="session-a")

    assert paths.home == home.resolve()
    assert host_root(home) == home.resolve() / "host"
    assert paths.host_root == home.resolve() / "host"
    assert paths.secrets_root == paths.host_root / ".secrets"
    assert paths.workspace == paths.host_root / "workspace"
    assert paths.mcp_config_file == paths.workspace / ".mcp"
    assert paths.skills == paths.workspace / "skills"
    assert paths.sessions_root == paths.workspace / "sessions"
    assert paths.console_root == paths.sessions_root / "console"
    assert paths.dag_root == paths.sessions_root / "dag"
    assert paths.session_root == paths.sessions_root / "session-a"
    assert paths.data_root == paths.workspace / "data"
    assert paths.artifacts_root == paths.workspace / "artifacts"
    assert paths.artifact_dir == paths.artifacts_root / "session-a"
    assert paths.node_artifact_dir("graph-a", "node-a") == (
        paths.artifact_dir / "graph-a" / "node-a"
    )
    assert not home.exists()


def test_paths_reject_legacy_agent_id_shape(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Paths(tmp_path, "legacy-agent", "session-a")  # type: ignore[call-arg]

    with pytest.raises(TypeError, match="agent_id"):
        Paths(  # type: ignore[call-arg]
            tmp_path,
            agent_id="legacy-agent",
            session_id="session-a",
        )
