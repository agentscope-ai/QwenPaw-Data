from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data_auth.auth_api import login_fails_by_ip, login_fails_by_user
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import reset_engines
from qwenpaw_data_auth.server import create_app


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for name in ("QWENPAW_DATA_AUTH_SECRET", "QWENPAW_DATA_SERVICE_SECRET",
                 "QWENPAW_DATA_API_TOKEN", "QWENPAW_DATA_DB_URL", "QWENPAW_DATA_AUTH_URL",
                 "QWENPAW_DATA_PROVISION_BACKENDS", "NEO4J_URI", "NEO4J_PASSWORD", "HOLO_AGE_HOST", "HOLO_AGE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    # Ported source tests assume multi-user login/RBAC; local-mode tests opt out via ``local_mode``.
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    login_fails_by_ip.clear()
    login_fails_by_user.clear()
    reset_engines()
    yield
    reset_engines()


@pytest.fixture
def local_mode(isolated_runtime, monkeypatch):
    """Run under the shipped default: QWENPAW_DATA_AUTH_MODE unset, i.e. local mode."""
    monkeypatch.delenv("QWENPAW_DATA_AUTH_MODE", raising=False)


@pytest.fixture
def multi_config(tmp_path):
    return AuthConfig(home=tmp_path / "multi", mode="multi-user", secret_keys=["test-signing-key"],
                      service_secret="test-machine-key", cm_base_url="", platform_bootstrap_token="test-bootstrap-key",
                      cors_allowed_origins=["http://localhost:3000"], authorization_redirect_origins=["http://localhost:3000"])


@pytest.fixture
def multi_client(multi_config):
    with TestClient(create_app(multi_config), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client
