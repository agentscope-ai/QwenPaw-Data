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
    with pytest.raises(ValidationError, match=name.removeprefix("ACS_").lower()):
        AcsE2BWorkspace(paths=workspace.paths, workspace_id="invalid")


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
