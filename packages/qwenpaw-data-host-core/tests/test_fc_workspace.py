import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.workspace.fc_workspace import FcE2BWorkspace, _oss_metadata


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    async_sandbox = SimpleNamespace(create=AsyncMock(), list=Mock())
    monkeypatch.setitem(
        sys.modules,
        "e2b",
        SimpleNamespace(AsyncSandbox=async_sandbox),
    )
    for key in ("FC_VPC_ID", "FC_VSWITCH_IDS", "FC_SECURITY_GROUP_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "FC_TEMPLATE_ID": "qwenpaw-data-prebuilt",
        "FC_API_KEY": "secret-api-key",
        "FC_API_URL": "https://api.cn-beijing.e2b.fc.aliyuncs.com",
        "FC_DOMAIN": "cn-beijing.e2b.fc.aliyuncs.com",
        "FC_ROLE_ARN_PATTERN": "acs:ram::123456:role/{uid}",
        "QWENPAW_DATA_WORKSPACE_OSS_BUCKET": "test-bucket",
        "QWENPAW_DATA_WORKSPACE_OSS_PREFIX": "test",
        "QWENPAW_DATA_WORKSPACE_OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
    }.items():
        monkeypatch.setenv(key, value)
    clear_settings()
    paths = UserAgentPaths(tmp_path, "tenant", "workspace", "user", "agent", "session")
    return FcE2BWorkspace(
        paths=paths,
        workspace_id="test-workspace",
    )


@pytest.mark.parametrize(
    ("vswitch_ids", "expected_vswitch_ids"),
    [
        (None, None),
        ("vsw-one", ["vsw-one"]),
        (" vsw-one , vsw-two ", ["vsw-one", "vsw-two"]),
    ],
)
async def test_provision_metadata_and_paths(
    workspace, monkeypatch, vswitch_ids, expected_vswitch_ids
):
    if vswitch_ids is not None:
        monkeypatch.setenv("FC_VPC_ID", " vpc-test ")
        monkeypatch.setenv("FC_VSWITCH_IDS", vswitch_ids)
        monkeypatch.setenv("FC_SECURITY_GROUP_ID", " sg-test ")
        clear_settings()
        workspace = FcE2BWorkspace(paths=workspace.paths, workspace_id="test-workspace")
    monkeypatch.setenv("PATH", "/host-only/bin")
    monkeypatch.setenv("E2B_API_KEY", "legacy-key")
    monkeypatch.setenv("E2B_API_URL", "https://legacy.invalid")
    monkeypatch.setenv("E2B_DOMAIN", "legacy.invalid")
    sandbox = SimpleNamespace(
        sandbox_id="sandbox", is_running=AsyncMock(return_value=True)
    )
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("e2b.AsyncSandbox.create", create)
    monkeypatch.setattr(
        "e2b.AsyncSandbox.list",
        Mock(side_effect=AssertionError("FC must create a fresh sandbox")),
    )
    await workspace._provision_backend()
    assert await workspace.get_backend().getcwd() == "/qwenpaw_data/agent/workspace"
    kwargs = create.call_args.kwargs
    assert kwargs["api_key"] == "secret-api-key"
    assert kwargs["api_url"] == "https://api.cn-beijing.e2b.fc.aliyuncs.com"
    assert kwargs["domain"] == "cn-beijing.e2b.fc.aliyuncs.com"
    sandbox_path = kwargs["envs"]["PATH"].split(":")
    assert sandbox_path[0] == workspace._gateway_venv + "/bin"
    assert {"/usr/local/bin", "/usr/bin", "/bin"} <= set(sandbox_path)
    assert "/host-only/bin" not in sandbox_path and "$PATH" not in sandbox_path
    mount = json.loads(kwargs["metadata"]["fc.sandbox.storage.oss"])["mountPoints"][0]
    assert mount["bucketPath"] == "/test/tenant/workspace/user"
    assert kwargs["metadata"]["fc.sandbox.auth.role"] == "acs:ram::123456:role/user"
    if expected_vswitch_ids is None:
        assert "fc.sandbox.network.vpc" not in kwargs["metadata"]
    else:
        assert json.loads(kwargs["metadata"]["fc.sandbox.network.vpc"]) == {
            "vpcId": "vpc-test",
            "vSwitchIds": expected_vswitch_ids,
            "securityGroupId": "sg-test",
        }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("FC_VPC_ID", ""),
        ("FC_SECURITY_GROUP_ID", ""),
        ("FC_VSWITCH_IDS", " "),
        ("FC_VSWITCH_IDS", "vsw-one, ,vsw-two"),
    ],
)
async def test_invalid_vpc_fails_before_creation(workspace, monkeypatch, field, value):
    for name, configured in {
        "FC_VPC_ID": "vpc-test",
        "FC_VSWITCH_IDS": "vsw-one",
        "FC_SECURITY_GROUP_ID": "sg-test",
    }.items():
        monkeypatch.setenv(name, configured)
    monkeypatch.setenv(field, value)
    clear_settings()
    create = AsyncMock()
    monkeypatch.setattr("e2b.AsyncSandbox.create", create)
    with pytest.raises(ValueError, match=field):
        invalid = FcE2BWorkspace(paths=workspace.paths, workspace_id="invalid")
        await invalid._provision_backend()
    create.assert_not_called()


def test_oss_metadata_uses_per_user_role_and_prefix(workspace):
    metadata = _oss_metadata(replace(workspace.paths, user_id="bob"))
    assert metadata["fc.sandbox.auth.role"] == "acs:ram::123456:role/bob"
    mount = json.loads(metadata["fc.sandbox.storage.oss"])["mountPoints"][0]
    assert mount["bucketPath"] == "/test/tenant/workspace/bob"


@pytest.mark.parametrize(
    ("template", "error"),
    [("", "template_id"), (" ", "template_id"), ("base", "prebuilt")],
)
def test_requires_prebuilt_template(workspace, monkeypatch, template, error):
    monkeypatch.setenv("FC_TEMPLATE_ID", template)
    clear_settings()
    with pytest.raises(ValidationError, match=error):
        FcE2BWorkspace(paths=workspace.paths, workspace_id="invalid")


@pytest.mark.parametrize("pattern", [None, "static-role"])
def test_role_pattern_requires_uid(workspace, monkeypatch, pattern):
    monkeypatch.delenv("FC_ROLE_ARN_PATTERN")
    if pattern is not None:
        monkeypatch.setenv("FC_ROLE_ARN_PATTERN", pattern)
    clear_settings()
    with pytest.raises(ValidationError, match="role_arn_pattern"):
        FcE2BWorkspace(paths=workspace.paths, workspace_id="invalid")


@pytest.mark.parametrize(
    "error", [RuntimeError("startup failed"), asyncio.CancelledError()]
)
async def test_startup_propagates_failure_for_manager_cleanup(workspace, caplog, error):
    sandbox = SimpleNamespace(sandbox_id="starting", kill=AsyncMock())

    async def provision():
        workspace._sandbox = sandbox
        workspace._backend = SimpleNamespace(
            file_exists=AsyncMock(return_value=False),
            join_path=lambda *parts: "/".join(parts),
        )

    workspace._provision_backend = provision

    workspace._ensure_workspace_layout = AsyncMock(side_effect=error)
    with caplog.at_level("INFO"), pytest.raises(type(error)) as exc_info:
        await workspace.initialize()
    assert exc_info.value is error
    sandbox.kill.assert_not_awaited()
    assert "sandbox_id=starting" in caplog.text
    assert "outcome=failed" in caplog.text


@pytest.mark.parametrize(
    ("idle", "idle_ttl", "expected_timeout"),
    [(False, 900, 900), (True, 900, 960), (True, 60, 900)],
)
async def test_renewal_timeout(
    workspace, monkeypatch, idle, idle_ttl, expected_timeout
):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_SANDBOX_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_SANDBOX_IDLE_TTL_SECONDS", str(idle_ttl))
    clear_settings()
    workspace._sandbox = SimpleNamespace(set_timeout=AsyncMock())
    await workspace.renew(idle=idle)
    workspace._sandbox.set_timeout.assert_awaited_once_with(
        expected_timeout,
        request_timeout=10,
    )


async def test_cancelled_teardown_keeps_handle_for_shutdown_cleanup(workspace):
    sandbox = SimpleNamespace(
        kill=AsyncMock(side_effect=[asyncio.CancelledError, None])
    )
    workspace._sandbox = sandbox
    with pytest.raises(asyncio.CancelledError):
        await workspace._teardown_backend()
    assert workspace._sandbox is sandbox
    await workspace._teardown_backend()
    assert workspace._sandbox is None


@pytest.mark.parametrize("name", ["FC_API_KEY", "FC_API_URL", "FC_DOMAIN"])
def test_requires_connection_config(workspace, monkeypatch, name):
    monkeypatch.delenv(name)
    clear_settings()
    with pytest.raises(ValidationError, match=name.removeprefix("FC_").lower()):
        FcE2BWorkspace(paths=workspace.paths, workspace_id="invalid")

