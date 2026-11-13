"""Protect existing installations while unresolved legacy contracts stay open."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api import app as app_module
from qwenpaw_data.host.core.settings.db import validate_legacy_store_mode
from qwenpaw_data.host.core.settings.workspace import WorkspaceSettings


@pytest.mark.parametrize("value", [None, "", "sqlite", " SQLITE "])
def test_current_sql_mode_accepts_legacy_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    else:
        monkeypatch.setenv("QWENPAW_DATA_STORE", value)
    validate_legacy_store_mode()


@pytest.mark.parametrize("value", ["json", " JSON ", "unsupported"])
def test_unsupported_storage_stops_before_bootstrap_and_preserves_files(
    tmp_path, monkeypatch, value,
):
    home = tmp_path / "legacy-home"
    sessions = home / "host" / "sessions"
    sessions.mkdir(parents=True)
    legacy = sessions / "session.json"
    original = b'{"id":"existing-session","title":"Keep this"}\n'
    legacy.write_bytes(original)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    monkeypatch.setenv("QWENPAW_DATA_STORE", value)
    bootstrap = Mock(side_effect=AssertionError("must not initialize control-plane state"))
    monkeypatch.setattr(app_module, "bootstrap_local", bootstrap)
    with pytest.raises(RuntimeError, match="QWENPAW_DATA_STORE") as error:
        with TestClient(app_module.create_app()):
            pytest.fail("Unsupported storage started")
    if value.strip().lower() == "json":
        assert "has not been migrated" in str(error.value)
    bootstrap.assert_not_called()
    assert legacy.read_bytes() == original
    assert [p for p in home.rglob("*") if p.is_file()] == [legacy]


def test_default_execution_backend_is_docker_and_local_requires_opt_in(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_WORKSPACE_BACKEND", raising=False)
    assert WorkspaceSettings().backend == "docker"
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "local")
    assert WorkspaceSettings().backend == "local"


_STARTUP_STAGES = ["db", "recovery", "channels", "workspace", "cron", "tracer", "scores"]


@pytest.fixture
def lifecycle_probe(monkeypatch):
    """Exercise the real lifespan without external services or persistent state."""
    events = []
    faults = {}

    def step(name):
        def run():
            events.append(name)
            if name in faults:
                raise faults[name]
        return run

    def async_step(name):
        async def run():
            step(name)()
        return run

    def resource(name):
        return SimpleNamespace(
            start=async_step(f"start:{name}"),
            shutdown=async_step(f"stop:{name}"),
        )

    recovery = resource("recovery")
    channels = SimpleNamespace(
        start_all=async_step("start:channels"),
        stop_all=async_step("stop:channels"),
    )

    async def from_config():
        return channels

    scores = resource("scores")
    scores.start = step("start:scores")
    monkeypatch.setattr(app_module, "bootstrap_local", Mock(return_value=object()))
    monkeypatch.setattr(app_module, "get_session_factory", Mock(return_value=object()))
    monkeypatch.setattr(app_module, "init_db", async_step("start:db"))
    monkeypatch.setattr(app_module, "reset_engine", async_step("reset:db"))
    monkeypatch.setattr(app_module, "reset_hub", step("reset:hub"))
    monkeypatch.setattr(app_module, "ChatRecovery", Mock(return_value=recovery))
    monkeypatch.setattr(app_module, "ChannelManager", SimpleNamespace(from_config=from_config))
    monkeypatch.setattr(app_module, "get_workspace_manager", Mock(return_value=resource("workspace")))
    monkeypatch.setattr(app_module, "stop_workspace_manager", async_step("stop:workspace"))
    monkeypatch.setattr(app_module, "CronManager", Mock(return_value=resource("cron")))
    monkeypatch.setattr(app_module, "tracer_install", step("start:tracer"))
    monkeypatch.setattr(app_module, "tracer_shutdown", step("stop:tracer"))
    monkeypatch.setattr(app_module, "TracerScoreFlusher", Mock(return_value=scores))
    return events, faults


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", _STARTUP_STAGES)
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_partial_startup_unwinds_resources(lifecycle_probe, stage, error_type):
    events, faults = lifecycle_probe
    error = error_type(f"failed {stage}")
    faults[f"start:{stage}"] = error
    with pytest.raises(error_type) as caught:
        async with app_module.lifespan(app_module.create_app()):
            pytest.fail("failed startup must not serve requests")
    assert caught.value is error
    started = _STARTUP_STAGES[:_STARTUP_STAGES.index(stage) + 1]
    assert events == (
        ["reset:db", "reset:hub"]
        + [f"start:{name}" for name in started]
        + [f"stop:{name}" for name in reversed(started[1:])]
        + ["reset:hub", "reset:db"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_cleanup", [None, "scores", "cron", "workspace", "channels"])
async def test_shutdown_unwinds_even_when_a_service_fails(lifecycle_probe, failing_cleanup):
    events, faults = lifecycle_probe
    error = RuntimeError("shutdown failed")
    if failing_cleanup:
        faults[f"stop:{failing_cleanup}"] = error

    async def run():
        async with app_module.lifespan(app_module.create_app()):
            events.append("serving")

    if failing_cleanup:
        with pytest.raises(RuntimeError) as caught:
            await run()
        assert caught.value is error
    else:
        await run()
    assert events == (
        ["reset:db", "reset:hub"]
        + [f"start:{name}" for name in _STARTUP_STAGES]
        + ["serving"]
        + [f"stop:{name}" for name in reversed(_STARTUP_STAGES[1:])]
        + ["reset:hub", "reset:db"]
    )
