import json
import importlib.util
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.workspace.acs_workspace import AcsE2BWorkspace

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("e2b") is None,
    reason="ACS workspace requires the optional cloud SDK",
)

ACS_ENV = {
    "ACS_API_KEY": "e2b_0123abcd",
    "ACS_DOMAIN": "sandbox.example.com",
    "ACS_TEMPLATE": "qwenpaw-data-workspace",
    "ACS_OSS_PV": "qwenpaw-data-oss",
    "ACS_CREDENTIAL_PROVIDER": "qwenpaw-data-oss-rw",
    "ACS_AGENT_NAME": "qwenpaw-data-storage-agent",
}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    for key in ("ACS_API_URL", "ACS_SANDBOX_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        **ACS_ENV,
        "QWENPAW_DATA_WORKSPACE_OSS_PREFIX": "test",
    }.items():
        monkeypatch.setenv(key, value)
    clear_settings()
    paths = UserAgentPaths(tmp_path, "tenant", "workspace", "user", "agent", "session")
    yield AcsE2BWorkspace(paths=paths, workspace_id="test-workspace")
    clear_settings()


async def test_create_claims_sandbox_with_user_oss_mount(workspace, monkeypatch):
    sandbox = SimpleNamespace(
        sandbox_id="sandbox", is_running=AsyncMock(return_value=True)
    )
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("e2b.AsyncSandbox.create", create)
    monkeypatch.setattr(
        "e2b.AsyncSandbox.list",
        Mock(side_effect=AssertionError("ACS must claim a fresh sandbox")),
    )
    await workspace._provision_backend()

    assert await workspace.get_backend().getcwd() == "/qwenpaw_data/agent/workspace"
    kwargs = create.call_args.kwargs
    assert kwargs["template"] == "qwenpaw-data-workspace"
    assert kwargs["api_key"] == "e2b_0123abcd"
    assert kwargs["domain"] == "sandbox.example.com"
    assert "api_url" not in kwargs
    metadata = kwargs["metadata"]
    assert metadata["agentscope.workspace.id"] == "test-workspace"
    assert metadata["qwenpaw_data.workspace.kind"] == "user"
    assert metadata["security.agents.kruise.io/agent-name"] == "qwenpaw-data-storage-agent"
    assert json.loads(metadata["e2b.agents.kruise.io/csi-volume-config"]) == [
        {
            "pvName": "qwenpaw-data-oss",
            "mountPath": "/qwenpaw-data",
            "subPath": "test/tenant/workspace/user",
            "attributes": {"credentialProviderName": "qwenpaw-data-oss-rw"},
        }
    ]


def test_sub_path_is_per_user(workspace):
    other = AcsE2BWorkspace(
        paths=replace(workspace.paths, user_id="bob", agent_id="other"),
        workspace_id="other",
    )
    volume = json.loads(
        other.sandbox_metadata["e2b.agents.kruise.io/csi-volume-config"]
    )[0]
    assert volume["subPath"] == "test/tenant/workspace/bob"
    assert other.workdir == "/qwenpaw_data/other/workspace"


@pytest.mark.parametrize("name", sorted(ACS_ENV))
def test_requires_acs_config(workspace, monkeypatch, name):
    monkeypatch.delenv(name)
    clear_settings()
    match = "ACS_DOMAIN" if name == "ACS_DOMAIN" else name.removeprefix("ACS_").lower()
    with pytest.raises(ValidationError, match=match):
        AcsE2BWorkspace(paths=workspace.paths, workspace_id="invalid")


@pytest.mark.parametrize("domain", ["", "sandbox.example.com"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_explicit_urls_route_both_sdk_planes(workspace, monkeypatch, domain, scheme):
    from e2b.connection_config import ConnectionConfig

    api_url = f"{scheme}://10.0.1.10:8080"
    sandbox_url = f"{scheme}://10.0.1.11:7788"
    monkeypatch.setenv("ACS_DOMAIN", domain)
    monkeypatch.setenv("ACS_API_URL", f" {api_url} ")
    monkeypatch.setenv("ACS_SANDBOX_URL", f" {sandbox_url} ")
    monkeypatch.setenv("E2B_API_URL", "https://wrong-api.invalid")
    monkeypatch.setenv("E2B_SANDBOX_URL", "https://wrong-gateway.invalid")
    clear_settings()
    configured = AcsE2BWorkspace(paths=workspace.paths, workspace_id="http-workspace")
    sandbox = SimpleNamespace(sandbox_id="sandbox", is_running=AsyncMock(return_value=True))
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("e2b.AsyncSandbox.create", create)
    await configured._provision_backend()
    opts = configured._api_opts()
    assert create.call_args.kwargs["api_url"] == api_url
    assert create.call_args.kwargs["sandbox_url"] == sandbox_url
    connection = ConnectionConfig(**opts)
    assert connection.api_url == api_url
    assert connection.get_sandbox_url("sandbox", "returned-domain.invalid") == sandbox_url
    assert "api_url" not in workspace._api_opts()


@pytest.mark.parametrize("changes", [
    {"ACS_DOMAIN": " "},
    {"ACS_API_URL": "http://10.0.1.10:8080"},
    {"ACS_SANDBOX_URL": "http://10.0.1.11:7788"},
])
def test_incomplete_connection_config_fails_before_creation(workspace, monkeypatch, changes):
    for key, value in changes.items():
        monkeypatch.setenv(key, value)
    clear_settings()
    create = Mock()
    monkeypatch.setattr("e2b.AsyncSandbox.create", create)
    with pytest.raises(ValidationError):
        AcsE2BWorkspace(paths=workspace.paths, workspace_id="invalid")
    create.assert_not_called()


@pytest.mark.parametrize(
    ("backend", "cloud"),
    [
        ("local", False),
        ("docker", False),
        ("fc-sandbox", True),
        (" ACS-Sandbox ", True),
    ],
)
def test_backend_is_cloud_sandbox(monkeypatch, backend, cloud):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test")
    clear_settings()
    try:
        assert get_settings().workspace.is_cloud_sandbox is cloud
    finally:
        clear_settings()


def test_cloud_backend_requires_sandbox_cm_url(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "acs-sandbox")
    monkeypatch.delenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", raising=False)
    clear_settings()
    try:
        with pytest.raises(ValueError, match="QWENPAW_DATA_CM_SANDBOX_BASE_URL"):
            get_settings()
    finally:
        clear_settings()
