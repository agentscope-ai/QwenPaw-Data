import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from context_manager.settings import Settings, clear_settings, get_settings
from context_manager.settings.cron import CronSettings
from context_manager.settings.execution import ExecutionSettings, PoolSettings
from context_manager.settings.graph import GraphSettings
from context_manager.settings.heartbeat import HeartbeatSettings
from context_manager.settings.model import ModelSettings
from context_manager.settings.paths import PathsSettings
from context_manager.settings.runtime import RuntimeSettings
from qwenpaw_data_auth.config import local_master_secret as _local_master_secret


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_PREFS_MASTER_SECRET", raising=False)
    clear_settings()
    yield
    clear_settings()


def test_default_settings_keep_cloud_backends_opt_in():
    settings = Settings()
    assert settings.graph.backend == "neo4j"
    assert settings.execution.graph_read.workers == 8
    assert settings.execution.background.workers == 2
    assert settings.execution.sql.workers == 4
    assert settings.model.sdk_max_retries == 2
    assert settings.knowledge.ingest_llm_timeout == 600


def test_settings_cache_is_explicitly_reset():
    first = get_settings()
    assert get_settings() is first
    clear_settings()
    assert get_settings() is not first


def test_unbounded_model_timeout_still_bounds_ingestion():
    settings = Settings(model=ModelSettings(timeout=None))
    assert settings.knowledge.ingest_llm_timeout == 900


def test_execution_environment_updates_one_nested_field(monkeypatch):
    monkeypatch.setenv("CM_EXECUTION_SQL__WORKERS", "3")
    settings = ExecutionSettings()
    assert settings.sql.workers == 3
    assert settings.sql.max_waiters == 16
    assert settings.background.workers == 2


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_pool_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError):
        PoolSettings(workers=1, queue_timeout_seconds=value)


def test_heartbeat_stale_window_must_exceed_refresh_interval():
    with pytest.raises(ValidationError, match="greater than interval_seconds"):
        HeartbeatSettings(interval_seconds=30, stale_after_seconds=30)


@pytest.mark.parametrize("home", ["relative/home", " /tmp/home", "/tmp/home "])
def test_home_rejects_ambiguous_paths(home):
    with pytest.raises((ValidationError, RuntimeError)):
        PathsSettings(home=home)


def test_graph_backend_aliases_and_connection_parameters(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "holo-age")
    monkeypatch.setenv("HOLO_AGE_PASSWORD", "test-only-password")
    settings = GraphSettings()
    assert settings.backend == "holoage"
    assert settings.connection_params()["password"] == "test-only-password"
    assert "test-only-password" not in repr(settings)


def test_runtime_has_no_fixed_graph_password(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert RuntimeSettings(environment="development").neo4j_password == ""


def test_local_graph_route_cannot_select_another_workspace(monkeypatch):
    monkeypatch.setenv("CM_TENANT_ID", "other-tenant")
    monkeypatch.setenv("CM_WORKSPACE_ID", "other-workspace")
    assert set(RuntimeSettings().model_dump()) == {
        "neo4j_uri", "neo4j_user", "neo4j_password", "neo4j_database",
    }


def test_scheduler_can_be_disabled_without_a_valid_clock():
    settings = CronSettings.from_environment(
        {"CM_SCHEDULER_ENABLED": "off", "CM_SCHEDULER_HOUR": "99"}
    )
    assert not settings.enabled


def test_scheduler_validates_enabled_clock():
    with pytest.raises(ValueError):
        CronSettings.from_environment({"CM_SCHEDULER_HOUR": "99"})


def test_local_master_key_survives_restart(tmp_path):
    first = ModelSettings(env="development").master_key()
    assert len(first) == 32
    assert ModelSettings(env="development").master_key() == first
    key_path = tmp_path / "preferences.key"
    assert bytes.fromhex(key_path.read_text().strip()) == first
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".preferences-key.*"))


def test_different_installations_get_different_master_keys(tmp_path):
    assert _local_master_secret(tmp_path / "first") != _local_master_secret(
        tmp_path / "second"
    )


def test_concurrent_master_key_initialization_publishes_one_complete_key(tmp_path):
    home = tmp_path / "concurrent"
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: _local_master_secret(home), range(32)))
    assert len(set(keys)) == 1
    assert len(bytes.fromhex(keys[0])) == 32
    assert not list(home.glob(".preferences-key.*"))


def test_existing_invalid_master_key_is_not_replaced(tmp_path):
    key_path = tmp_path / "preferences.key"
    key_path.write_text("invalid-test-key")
    with pytest.raises(RuntimeError, match="64 hex characters") as error:
        ModelSettings(env="development").master_key()
    assert "invalid-test-key" not in str(error.value)
    assert key_path.read_text() == "invalid-test-key"


@pytest.mark.parametrize("environment", ["prod", "production", " PRODUCTION "])
def test_production_never_generates_a_preferences_key(environment, tmp_path):
    with pytest.raises(RuntimeError, match="must be set in production"):
        ModelSettings(env=environment).master_key()
    assert not (tmp_path / "preferences.key").exists()


def test_explicit_master_key_does_not_write_local_secrets(tmp_path):
    secret = "ab" * 32
    settings = ModelSettings(env="production", preferences_master_secret=secret)
    assert settings.master_key() == bytes.fromhex(secret)
    assert secret not in repr(settings)
    assert not (tmp_path / "preferences.key").exists()


@pytest.mark.parametrize("secret", ["ff", "g" * 64, "aa " * 32])
def test_invalid_master_key_errors_do_not_echo_input(secret):
    with pytest.raises(RuntimeError) as error:
        ModelSettings(preferences_master_secret=secret).master_key()
    assert secret not in str(error.value)
