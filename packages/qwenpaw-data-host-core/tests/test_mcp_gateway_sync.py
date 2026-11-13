from __future__ import annotations

import json
import posixpath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.workspace import DockerWorkspace, LocalWorkspace
from agentscope.workspace._gateway_client import GatewayClient, GatewayMCPClient

from qwenpaw_data.host.core.domain.mcp import McpConnectFailed
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import clear_settings


class _GatewayRequests:
    def __init__(self):
        self.registered = {}
        self.calls = []
        self.delete_error = None

    async def request(self, method, path, *, params, body=None):
        key = (params["agent_id"], params["session_id"])
        self.calls.append((method, path, key))
        if method == "POST":
            key = (*key, body["name"])
            if key in self.registered:
                return 409, '{"detail":"already exists"}'
            self.registered[key] = body
        elif method == "DELETE":
            if isinstance(self.delete_error, Exception):
                raise self.delete_error
            if self.delete_error is not None:
                return self.delete_error, '{"detail":"delete failed"}'
            del self.registered[(*key, path.rsplit("/", 1)[-1])]
        elif method == "GET":
            assert path.endswith("/tools")
            assert (*key, path.split("/")[2]) in self.registered
            return 200, "[]"
        elif method == "PUT":
            assert path.endswith("/runtime-headers")
            return 200, '{"ok":true}'
        else:
            raise AssertionError(f"unexpected request: {method} {path}")
        return 200, '{"ok":true}'


@pytest.fixture
def mcp_http(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "local")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    clear_settings()

    def respond(request):
        message = json.loads(request.content)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-cm", "version": "1"},
            }
        else:
            assert message["method"] == "tools/list"
            result = {"tools": []}
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": result,
            },
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs),
    )


@pytest.fixture
def gateway_sync(tmp_path, monkeypatch, mcp_http):
    # Keep the SDK's actual registration, deletion, locking and persistence
    # logic. Only replace gateway requests and sandbox file writes.
    workspace = DockerWorkspace(
        workspace_id="mcp-sync-test",
        host_workdir=str(tmp_path),
    )
    backend = SimpleNamespace(join_path=posixpath.join, write_file=AsyncMock())
    workspace._backend = backend
    workspace._gateway = GatewayClient(backend, gateway_port=8080)
    requests = _GatewayRequests()
    monkeypatch.setattr(workspace._gateway, "exec_request", requests.request)
    return SimpleNamespace(
        repo=McpRepository(tmp_path / "mcp.json"),
        workspace=workspace,
        requests=requests,
        storage=backend.write_file,
        scope={"agent_id": "default", "session_id": "session"},
    )


async def test_gateway_sync_reuses_then_replaces_configuration(gateway_sync):
    h = gateway_sync
    await h.repo.sync(h.workspace, **h.scope, request_id="chat-1", mcp_headers={})
    original = (await h.workspace.list_mcps(**h.scope))[0]
    assert isinstance(original, GatewayMCPClient)

    await h.repo.sync(h.workspace, **h.scope, request_id="chat-2", mcp_headers={})
    assert (await h.workspace.list_mcps(**h.scope))[0] is original
    assert [
        method for method, _, _ in h.requests.calls if method not in {"GET", "PUT"}
    ] == ["POST"]

    h.repo.set_tools("context-manager", None, ["changed-tool"])
    await h.repo.sync(h.workspace, **h.scope, request_id="chat-3", mcp_headers={})

    assert [
        method for method, _, _ in h.requests.calls if method not in {"GET", "PUT"}
    ] == [
        "POST",
        "DELETE",
        "POST",
    ]
    assert all(scope == ("default", "session") for _, _, scope in h.requests.calls)
    current = (await h.workspace.list_mcps(**h.scope))[0]
    assert current is not original
    assert not original.is_connected
    assert current.is_connected
    assert current.disable_tools == ["changed-tool"]
    assert len(h.requests.registered) == 1
    assert h.requests.registered[("default", "session", "context-manager")][
        "disable_tools"
    ] == ["changed-tool"]
    saved = json.loads(h.storage.call_args.args[1])
    assert saved["mcps"]["default"]["session"][0]["disable_tools"] == ["changed-tool"]


async def test_gateway_model_binding_is_ephemeral_and_cleared_next_turn(gateway_sync):
    h = gateway_sync
    await h.repo.sync(
        h.workspace, **h.scope, request_id="chat-1", mcp_headers={},
        context_model_header="private-turn-binding",
    )
    client = (await h.workspace.list_mcps(**h.scope))[0]
    assert client._runtime_headers["x-qwenpaw-model-binding"] == "private-turn-binding"
    await h.workspace._save_mcp_file()
    assert b"private-turn-binding" not in h.storage.call_args.args[1]
    assert "private-turn-binding" not in h.repo.manifest_path.read_text()
    await h.repo.sync(h.workspace, **h.scope, request_id="chat-2", mcp_headers={})
    assert (await h.workspace.list_mcps(**h.scope))[0] is client
    assert "x-qwenpaw-model-binding" not in client._runtime_headers


@pytest.mark.parametrize("stateful", [False, True])
async def test_gateway_sync_disables_without_duplicate_delete(gateway_sync, stateful):
    h = gateway_sync
    client = h.repo.require("context-manager").to_client()
    client.is_stateful = stateful
    await h.workspace.add_mcp(client, **h.scope)
    h.repo.disable("context-manager")

    await h.repo.sync(h.workspace, **h.scope, request_id="chat", mcp_headers={})

    assert [method for method, _, _ in h.requests.calls] == ["POST", "DELETE"]
    assert h.requests.registered == {}
    assert await h.workspace.list_mcps(**h.scope) == []
    assert json.loads(h.storage.call_args.args[1])["mcps"]["default"]["session"] == []


@pytest.mark.parametrize(
    "action,error",
    [
        ("replace", 500),
        ("replace", ConnectionError("delete failed")),
        ("disable", 500),
    ],
)
async def test_gateway_delete_failure_preserves_registration(
    gateway_sync, action, error
):
    h = gateway_sync
    await h.repo.sync(h.workspace, **h.scope, request_id="chat-1", mcp_headers={})
    original = (await h.workspace.list_mcps(**h.scope))[0]
    registered = dict(h.requests.registered)
    saved = h.storage.call_args
    h.storage.reset_mock()
    h.requests.delete_error = error
    if action == "replace":
        h.repo.set_tools("context-manager", None, ["changed-tool"])
        expected_error = McpConnectFailed
    else:
        h.repo.disable("context-manager")
        expected_error = RuntimeError

    with pytest.raises(expected_error, match="delete failed"):
        await h.repo.sync(h.workspace, **h.scope, request_id="chat-2", mcp_headers={})

    assert [method for method, _, _ in h.requests.calls] == [
        "POST",
        "PUT",
        "GET",
        "DELETE",
    ]
    assert h.requests.registered == registered
    assert (await h.workspace.list_mcps(**h.scope))[0] is original
    assert original.is_connected
    h.storage.assert_not_awaited()
    # Persisting again must still write the original declarations.
    await h.workspace._save_mcp_file()
    assert h.storage.call_args == saved


async def test_local_sync_replaces_and_disables(tmp_path, mcp_http):
    repo = McpRepository(tmp_path / "mcp.json")
    workspace = LocalWorkspace(workdir=str(tmp_path))
    scope = {"agent_id": "default", "session_id": "session"}
    try:
        await workspace.initialize()
        await repo.sync(workspace, **scope, request_id="chat-1", mcp_headers={})
        repo.set_tools("context-manager", None, ["changed-tool"])
        await repo.sync(workspace, **scope, request_id="chat-2", mcp_headers={})
        current = (await workspace.list_mcps(**scope))[0]
        assert not isinstance(current, GatewayMCPClient)
        assert current.disable_tools == ["changed-tool"]
        repo.disable("context-manager")
        await repo.sync(workspace, **scope, request_id="chat-3", mcp_headers={})
        assert await workspace.list_mcps(**scope) == []
    finally:
        await workspace.close()
