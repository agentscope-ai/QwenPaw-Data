# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
import pytest
from agentscope.mcp import MCPClient
from agentscope.workspace import LocalWorkspace
from agentscope.workspace._gateway_client import GatewayClient
from agentscope.workspace._mcp_gateway._mcp_gateway_app import _build_client

from qwenpaw_data.host.core.integrations.cm import ensure_workspace_cm_mcp
from qwenpaw_data.host.core.integrations.cm.mcp import project_workspace_mcp_client
from qwenpaw_data.host.core.domain.mcp import (
    Mcp,
    McpConflictError,
    McpConnectFailed,
    McpNotFoundError,
)
from qwenpaw_data.host.core.api.models.stream_objects import parse_stream_object
from qwenpaw_data.host.core.query_log import Step
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.utils.mcp import (
    load_builtin_mcp_records,
    public_tool,
    write_mcp_manifest,
)


def _repo(tmp_path: Path) -> McpRepository:
    return McpRepository(tmp_path / "mcp.json")


def _http(name: str, url: str, **host: object) -> Mcp:
    return Mcp(
        client=MCPClient(
            name=name,
            is_stateful=False,
            mcp_config={"type": "http_mcp", "url": url, "headers": {}, "timeout": 30},
        ),
        enabled=bool(host.get("enabled", True)),
        source=str(host.get("source", "custom")),
        description=str(host.get("description", "")),
    )


def test_list_seeds_builtin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    items = repo.list()
    assert items[0].name == "context-manager"
    assert items[0].source == "builtin"
    assert items[0].client.mcp_config.url == get_settings().cm.mcp_url()
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcps"]["context-manager"]["mcp_config"]["url"] == ""
    assert saved["mcps"]["context-manager"]["disable_tools"] == [
        "execute_create",
        "execute_insert",
        "execute_update",
        "execute_modify",
    ]
    assert items[0].client.disable_tools == [
        "execute_create",
        "execute_insert",
        "execute_update",
        "execute_modify",
    ]
    assert load_builtin_mcp_records()["context-manager"]["mcp_config"]["url"] == ""


def test_cm_url_always_comes_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.settings.test")
    clear_settings()
    try:
        repo = _repo(tmp_path)
        repo.list()
        payload = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        payload["mcps"]["context-manager"]["mcp_config"]["url"] = (
            "http://stale.example/mcp/v1/cm"
        )
        (tmp_path / "mcp.json").write_text(json.dumps(payload), encoding="utf-8")
        wanted = "http://cm.settings.test/mcp/v1/cm"
        assert repo.require("context-manager").client.mcp_config.url == wanted
        stale = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        assert stale["mcps"]["context-manager"]["mcp_config"]["url"] == (
            "http://stale.example/mcp/v1/cm"
        )
        repo.enable("context-manager")
        monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.next.test")
        clear_settings()
        assert repo.require("context-manager").client.mcp_config.url == (
            "http://cm.next.test/mcp/v1/cm"
        )
    finally:
        monkeypatch.delenv("QWENPAW_DATA_CM_BASE_URL", raising=False)
        clear_settings()


def test_create_update_delete_custom(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    created = repo.create(_http("search", "http://a/mcp", description="a"))
    assert created.name == "search"
    with pytest.raises(McpConflictError):
        repo.create(_http("search", "http://b/mcp"))
    updated = repo.update(
        _http("search", "http://b/mcp", enabled=False, description="b"),
    )
    assert updated.client.mcp_config.url == "http://b/mcp"
    assert updated.enabled is False
    assert updated.description == "b"
    repo.delete("search")
    with pytest.raises(McpNotFoundError):
        repo.require("search")


def test_set_tools_persists_disable_list(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mcp = repo.set_tools("context-manager", None, ["execute_insert"])
    assert mcp.client.enable_tools is None
    assert mcp.client.disable_tools == ["execute_insert"]
    assert repo.require("context-manager").client.disable_tools == ["execute_insert"]


def test_public_tool_honors_disable_list() -> None:
    tool = SimpleNamespace(name="execute_insert", description="", inputSchema={})
    assert (
        public_tool(
            tool,
            enable_tools=None,
            disable_tools=["execute_insert"],
        )["enabled"]
        is False
    )
    assert (
        public_tool(
            tool,
            enable_tools=None,
            disable_tools=["execute_create"],
        )["enabled"]
        is True
    )


def test_write_strips_authorization(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    write_mcp_manifest(
        path,
        {
            "search": {
                "enabled": True,
                "source": "custom",
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "http://a/mcp",
                    "headers": {"Authorization": "Bearer secret", "X-A": "1"},
                },
            }
        },
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    headers = saved["mcps"]["search"]["mcp_config"]["headers"]
    assert "Authorization" not in headers
    assert headers["X-A"] == "1"


@pytest.mark.asyncio
async def test_sync_adds_removes_and_skips_token_only_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    repo.create(_http("search", "http://a/mcp"))
    _stub_probe(monkeypatch)
    scope = {"agent_id": "agent", "session_id": "session", "request_id": "chat_1"}

    workspace = _FakeWorkspace()
    await repo.sync(workspace, mcp_headers={}, **scope)
    names = {client.name for client in workspace.clients}
    assert names == {"context-manager", "search"}

    live = next(client for client in workspace.clients if client.name == "search")
    live.mcp_config.headers = {"Authorization": "Bearer fresh"}
    await repo.sync(workspace, mcp_headers={}, **scope)
    search = next(client for client in workspace.clients if client.name == "search")
    assert search.mcp_config.headers["Authorization"] == "Bearer fresh"

    repo.update(_http("search", "http://b/mcp"))
    await repo.sync(workspace, mcp_headers={}, **scope)
    search = next(client for client in workspace.clients if client.name == "search")
    assert search.mcp_config.url == "http://b/mcp"

    repo.delete("search")
    await repo.sync(workspace, mcp_headers={}, **scope)
    assert {client.name for client in workspace.clients} == {"context-manager"}


@pytest.mark.parametrize(
    "backend,expected_url",
    [
        ("local", "http://127.0.0.1:8080/mcp/v1/cm"),
        ("docker", "http://host.docker.internal:8080/mcp/v1/cm"),
        ("fc-sandbox", "https://gateway.example.test/qwenpaw_data/mcp/v1/cm"),
        ("acs-sandbox", "https://gateway.example.test/qwenpaw_data/mcp/v1/cm"),
    ],
)
async def test_registration_headers_reach_handshake_and_survive_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    expected_url: str,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv(
        "QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test/qwenpaw_data/"
    )
    clear_settings()
    repo = _repo(tmp_path)
    cm = repo.require("context-manager")
    cm.client.mcp_config.headers = {
        "X-DataAgent-Service-Id": "old-service",
        "X-Keep": "static",
    }
    repo.update(cm)
    manifest_before = repo.manifest_path.read_bytes()
    original_client = cm.client.model_dump()
    projected = project_workspace_mcp_client(cm.client)
    assert projected.mcp_config.url == expected_url
    assert projected.mcp_config.headers == cm.client.mcp_config.headers
    assert cm.client.model_dump() == original_client
    custom = _http("custom", "http://127.0.0.1:9999/mcp").client
    assert project_workspace_mcp_client(custom) is custom
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        requests.append(request)
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

    class GatewayWorkspace(LocalWorkspace):
        async def add_mcp(self, client: MCPClient, **scope) -> None:
            # Exercise the sandbox's actual deserialization and handshake.
            remote = await _build_client(client.model_dump(mode="json"))
            await super().add_mcp(remote, **scope)

    scope = {"agent_id": "agent", "session_id": "session"}
    workspace = GatewayWorkspace(workdir=str(tmp_path))
    try:
        await workspace.initialize()
        await repo.sync(
            workspace,
            request_id="chat_1",
            **scope,
            mcp_headers={
                "context-manager": {
                    "x-dataagent-service-id": "cm-service-123",
                    "X-DataAgent-Uid": "user-123",
                    "sts-token": "test-sts-token",
                }
            },
        )
        await ensure_workspace_cm_mcp(
            workspace,
            access_token="runtime-only",
            chat_id="chat_1",
            **scope,
            datasource_id="source-1",
            dataset_names=["orders", "users"],
        )
        clients = await workspace.list_mcps(**scope)
        await clients[0].list_raw_tools()
        assert requests[-1].headers["Authorization"] == "Bearer runtime-only"
        assert requests[-1].headers["chat_id"] == "chat_1"
        assert requests[-1].headers["session_id"] == "session"
        assert requests[-1].headers["datasource_id"] == "source-1"
        assert requests[-1].headers["dataset_name"] == "orders,users"
    finally:
        await workspace.close()

    assert [json.loads(r.content)["method"] for r in requests[:3]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert "runtime-only" not in (tmp_path / ".mcp").read_text()
    assert repo.require("context-manager").client.mcp_config.headers == {
        "X-DataAgent-Service-Id": "old-service",
        "X-Keep": "static",
    }

    restored = LocalWorkspace(workdir=str(tmp_path))
    try:
        await restored.initialize()
        clients = await restored.list_mcps(**scope)
        assert len(clients) == 1
        await clients[0].list_raw_tools()
    finally:
        await restored.close()

    for request in requests:
        assert str(request.url) == expected_url
        assert request.headers.get_list("X-DataAgent-Service-Id") == ["cm-service-123"]
        assert request.headers["X-DataAgent-Uid"] == "user-123"
        assert request.headers["sts-token"] == "test-sts-token"
        assert request.headers["X-Keep"] == "static"
    assert "Authorization" not in requests[0].headers
    assert "Authorization" not in requests[-1].headers
    assert repo.manifest_path.read_bytes() == manifest_before

    # Administrative tool discovery runs on Host, even with an FC workspace.
    requests.clear()
    await repo.list_tools("context-manager", access_token="host-token")
    assert requests
    assert all(str(r.url) == "http://127.0.0.1:8080/mcp/v1/cm" for r in requests)
    assert all(r.headers["Authorization"] == "Bearer host-token" for r in requests)


async def test_registration_headers_support_custom_mcp_and_skip_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_probe(monkeypatch)
    repo = _repo(tmp_path)
    repo.disable("context-manager")
    repo.create(_http("search", "http://a/mcp"))
    workspace = _FakeWorkspace()
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="session",
        request_id="chat_1",
        mcp_headers={
            "context-manager": {"X-DataAgent-Service-Id": "disabled"},
            "search": {"X-Custom": "value"},
            "unknown": {"X-Custom": "ignored"},
        },
    )
    assert [client.name for client in workspace.clients] == ["search"]
    assert workspace.clients[0].mcp_config.headers == {"x-custom": "value"}


@pytest.mark.asyncio
async def test_list_tools_connects_fresh_client_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # stdio only reaches this path when explicitly opted in (local dev);
    # the default deployment rejects the registration outright.
    monkeypatch.setenv("QWENPAW_DATA_MCP_ALLOW_CUSTOM_STDIO", "1")
    clear_settings()
    repo = _repo(tmp_path)
    repo.create(
        Mcp(
            client=MCPClient(
                name="weather",
                is_stateful=True,
                mcp_config={
                    "type": "stdio_mcp",
                    "command": "npx",
                    "args": ["-y", "x"],
                },
            ),
            enabled=True,
            source="custom",
        ),
    )
    calls: list[str] = []

    async def connect(self: MCPClient) -> None:
        calls.append("connect")
        self._is_connected = True

    async def list_raw_tools(self: MCPClient) -> list:
        calls.append("list")
        self._cached_tools = [SimpleNamespace(name="ping")]
        return self._cached_tools

    async def close(self: MCPClient, ignore_errors: bool = True) -> None:
        _ = ignore_errors
        calls.append("close")
        self._is_connected = False

    monkeypatch.setattr(MCPClient, "connect", connect)
    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)
    monkeypatch.setattr(MCPClient, "close", close)
    mcp, tools = await repo.list_tools("weather")
    assert calls == ["connect", "list", "close"]
    assert mcp.name == "weather"
    assert [tool.name for tool in tools] == ["ping"]


@pytest.mark.asyncio
async def test_list_tools_stamps_cm_runtime_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    captured: list[dict[str, str]] = []

    async def list_raw_tools(self: MCPClient) -> list:
        captured.append(dict(self._runtime_headers))
        self._cached_tools = [SimpleNamespace(name="ping")]
        return self._cached_tools

    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)
    mcp, tools = await repo.list_tools("context-manager", access_token="tok")
    assert mcp.name == "context-manager"
    assert captured == [{"Authorization": "Bearer tok"}]
    assert "Authorization" not in (mcp.client.mcp_config.headers or {})
    assert [tool.name for tool in tools] == ["ping"]


class _FakeWorkspace:
    def __init__(self) -> None:
        self.clients: list[MCPClient] = []

    async def list_mcps(self, *, agent_id: str, session_id: str) -> list[MCPClient]:
        return list(self.clients)

    async def add_mcp(
        self, client: MCPClient, *, agent_id: str, session_id: str
    ) -> None:
        self.clients = [item for item in self.clients if item.name != client.name]
        self.clients.append(client)

    async def remove_mcp(self, name: str, *, agent_id: str, session_id: str) -> None:
        self.clients = [item for item in self.clients if item.name != name]


async def test_sync_probes_registered_gateway_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "http://sandbox-only.test")
    clear_settings()
    host_probe = AsyncMock(side_effect=AssertionError("MCP probe ran on Host"))
    monkeypatch.setattr(MCPClient, "list_raw_tools", host_probe)
    gateway = GatewayClient(SimpleNamespace(), gateway_port=8080)
    gateway_request = AsyncMock(return_value=(200, "[]"))
    monkeypatch.setattr(gateway, "exec_request", gateway_request)

    class GatewayWorkspace(_FakeWorkspace):
        async def add_mcp(self, client: MCPClient, **scope) -> None:
            proxy = gateway.make_client(
                client.model_dump(mode="json"),
                connected=True,
                **scope,
            )
            await super().add_mcp(proxy, **scope)

    repo = _repo(tmp_path)
    workspace = GatewayWorkspace()
    scope = {"agent_id": "agent", "session_id": "session"}
    await repo.sync(workspace, request_id="chat_1", mcp_headers={}, **scope)
    original = workspace.clients[0]
    assert original.mcp_config.url == "http://sandbox-only.test/mcp/v1/cm"

    await repo.sync(workspace, request_id="chat_2", mcp_headers={}, **scope)
    assert workspace.clients[0] is original

    await repo.sync(
        workspace,
        request_id="chat_3",
        **scope,
        mcp_headers={"context-manager": {"X-Service": "updated"}},
    )
    assert workspace.clients[0] is not original
    assert workspace.clients[0].mcp_config.headers["x-service"] == "updated"
    host_probe.assert_not_awaited()
    assert gateway_request.await_args_list == [
        call(
            "PUT",
            "/mcps/context-manager/runtime-headers",
            params=scope,
            body={"headers": {"session_id": "session", "chat_id": "chat_1"}},
        ),
        call("GET", "/mcps/context-manager/tools", params=scope),
        call(
            "PUT",
            "/mcps/context-manager/runtime-headers",
            params=scope,
            body={"headers": {"session_id": "session", "chat_id": "chat_2"}},
        ),
        call("GET", "/mcps/context-manager/tools", params=scope),
        call("DELETE", "/mcps/context-manager", params=scope),
        call(
            "PUT",
            "/mcps/context-manager/runtime-headers",
            params=scope,
            body={"headers": {"session_id": "session", "chat_id": "chat_3"}},
        ),
        call("GET", "/mcps/context-manager/tools", params=scope),
    ]


@pytest.mark.asyncio
async def test_sync_injects_configured_headers_for_inprocess_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cron and IM runs call ChatRuntime.run without mcp_headers, so the
    # request overlay never reaches them; the configured headers must land on
    # the CM spec before registration.
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "http://sandbox-only.test")
    monkeypatch.setenv(
        "QWENPAW_DATA_MCP_REGISTRATION_HEADERS",
        json.dumps(
            {
                "context-manager": {
                    "X-DataAgent-Service-Id": "svc-123",
                    "X-DataAgent-Uid": "123456",
                }
            }
        ),
    )
    clear_settings()
    repo = _repo(tmp_path)
    _stub_probe(monkeypatch)
    workspace = _FakeWorkspace()
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="session",
        request_id="chat_1",
        mcp_headers={},
    )
    cm = next(c for c in workspace.clients if c.name == "context-manager")
    assert cm.mcp_config.headers["x-dataagent-service-id"] == "svc-123"
    assert cm.mcp_config.headers["x-dataagent-uid"] == "123456"

    # An interactive gateway overlay wins per header and leaves the rest.
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="session",
        request_id="chat_2",
        mcp_headers={"context-manager": {"x-dataagent-service-id": "overlay-service"}},
    )
    cm = next(c for c in workspace.clients if c.name == "context-manager")
    assert cm.mcp_config.headers["x-dataagent-service-id"] == "overlay-service"
    assert cm.mcp_config.headers["x-dataagent-uid"] == "123456"


@pytest.mark.asyncio
async def test_sync_without_configured_headers_adds_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unset: behaves exactly as before the mechanism existed.
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "http://sandbox-only.test")
    monkeypatch.delenv("QWENPAW_DATA_MCP_REGISTRATION_HEADERS", raising=False)
    clear_settings()
    repo = _repo(tmp_path)
    _stub_probe(monkeypatch)
    workspace = _FakeWorkspace()
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="session",
        request_id="chat_1",
        mcp_headers={},
    )
    cm = next(c for c in workspace.clients if c.name == "context-manager")
    assert not cm.mcp_config.headers


class _FakeStream:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def mcp_connect(self, **fields: object) -> None:
        self.events.append(fields)


def _capture_query_log(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []

    def _write(fn):
        def _inner(**kwargs):
            recorded.append({"level": fn, **kwargs})

        return _inner

    monkeypatch.setattr("qwenpaw_data.host.core.repositories.mcp.log_info", _write("info"))
    monkeypatch.setattr("qwenpaw_data.host.core.repositories.mcp.log_error", _write("error"))
    return recorded


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None
) -> None:
    async def list_raw_tools(self: MCPClient) -> list:
        if error is not None:
            raise error
        return [SimpleNamespace(name="ping")]

    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)


@pytest.mark.asyncio
async def test_sync_stamps_cm_headers_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    workspace = _FakeWorkspace()
    seen: list[dict[str, str]] = []

    async def list_raw_tools(self: MCPClient) -> list:
        seen.append(dict(self._runtime_headers))
        return []

    monkeypatch.setattr(MCPClient, "list_raw_tools", list_raw_tools)
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="ses-old",
        request_id="chat-old",
        mcp_headers={},
        access_token="expired-token",
    )
    await repo.sync(
        workspace,
        agent_id="agent",
        session_id="ses-1",
        request_id="chat-2",
        mcp_headers={},
        access_token="fresh-token",
        datasource_id="postgresql-a",
        dataset_names=["orders"],
    )
    assert seen[-1] == {
        "Authorization": "Bearer fresh-token",
        "datasource_id": "postgresql-a",
        "dataset_name": "orders",
        "session_id": "ses-1",
        "chat_id": "chat-2",
    }


@pytest.mark.asyncio
async def test_sync_logs_mcp_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    repo.create(_http("search", "http://a/mcp"))
    recorded = _capture_query_log(monkeypatch)
    _stub_probe(monkeypatch)
    await repo.sync(
        _FakeWorkspace(),
        agent_id="agent",
        session_id="session",
        request_id="chat_1",
        mcp_headers={},
    )
    names = {item["context"]["mcp_name"] for item in recorded}
    assert names == {"context-manager", "search"}
    assert {item["step"] for item in recorded} == {Step.MCP_CONNECT}
    assert all(item["context"]["tool_count"] == 1 for item in recorded)


@pytest.mark.asyncio
async def test_sync_logs_mcp_connect_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    repo.create(_http("search", "http://a/mcp"))
    recorded = _capture_query_log(monkeypatch)
    _stub_probe(monkeypatch)

    class Boom(_FakeWorkspace):
        async def add_mcp(
            self, client: MCPClient, *, agent_id: str, session_id: str
        ) -> None:
            if client.name == "search":
                raise ConnectionError("refused")
            await super().add_mcp(client, agent_id=agent_id, session_id=session_id)

    stream = _FakeStream()
    await repo.sync(
        Boom(),
        agent_id="agent",
        session_id="session",
        request_id="chat_1",
        mcp_headers={},
        stream=stream,
    )
    assert [item["mcp_name"] for item in stream.events] == ["context-manager", "search"]
    assert stream.events[0]["status"] == "connected"
    assert stream.events[1] == {
        "mcp_name": "search",
        "status": "failed",
        "error": "refused",
        "url": "http://a/mcp",
    }
    failed = [item for item in recorded if item["step"] == Step.MCP_CONNECT_FAILED]
    assert len(failed) == 1
    assert failed[0]["context"]["mcp_name"] == "search"


@pytest.mark.asyncio
async def test_sync_logs_mcp_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    recorded = _capture_query_log(monkeypatch)
    _stub_probe(monkeypatch, error=ConnectionError("cm down"))
    stream = _FakeStream()
    with pytest.raises(McpConnectFailed, match="cm down"):
        await repo.sync(
            _FakeWorkspace(),
            agent_id="agent",
            session_id="session",
            request_id="chat_1",
            mcp_headers={},
            stream=stream,
        )
    assert stream.events[0]["mcp_name"] == "context-manager"
    assert stream.events[0]["status"] == "failed"
    assert stream.events[0]["error"] == "cm down"
    failed = [item for item in recorded if item["step"] == Step.MCP_CONNECT_FAILED]
    assert len(failed) == 1
    assert failed[0]["context"]["mcp_name"] == "context-manager"
    assert failed[0]["context"]["error"] == "cm down"


def test_parse_mcp_connect_failed_event() -> None:
    obj = parse_stream_object(
        {
            "object": "mcp.connect",
            "sequence_number": 1,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "created_at": "2026-11-04T08:00:00+00:00",
            "mcp_name": "search",
            "status": "failed",
            "error": "refused",
            "url": "http://a/mcp",
        }
    )
    assert obj.object == "mcp.connect"
    assert obj.status == "failed"
    assert obj.mcp_name == "search"
    connected = parse_stream_object(
        {
            "object": "mcp.connect",
            "sequence_number": 2,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "created_at": "2026-11-04T08:00:01+00:00",
            "mcp_name": "context-manager",
            "status": "connected",
            "url": "http://cm/mcp",
        }
    )
    assert connected.status == "connected"
    assert connected.error is None
