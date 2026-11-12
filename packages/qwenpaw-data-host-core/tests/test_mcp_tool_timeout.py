from __future__ import annotations

import asyncio
import base64
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from agentscope.mcp import MCPClient
from agentscope.message import ToolResultState
from agentscope.tool import ExecResult, ToolChunk
from agentscope.workspace import DockerBackend, E2BBackend
from agentscope.workspace._gateway_client import GatewayClient

from qwenpaw_data.host.core.agent.middleware.tool_timeout import ToolTimeoutMiddleware
from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
from qwenpaw_data.host.core.utils.mcp import bind_mcp_tool_timeout


class _Requests:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.fail_tool = False

    async def execute(self, argv, timeout):
        method, url = argv[3:5]
        parsed = urlsplit(url)
        self.calls.append(
            (method, parsed.path, timeout, parse_qs(parsed.query), argv[8])
        )
        if parsed.path == "/health":
            payload = {"instance_nonce": "gateway-instance"}
        elif method == "GET":
            payload = [{"name": "search_context", "inputSchema": {"type": "object"}}]
        else:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            if self.fail_tool:
                raise RuntimeError("context deadline exceeded")
            payload = {
                "chunk": ToolChunk(
                    content=[{"type": "text", "text": "result"}],
                    state=ToolResultState.SUCCESS,
                ).model_dump(mode="json")
            }
        return json.dumps(
            {
                "status": 200,
                "body": base64.b64encode(json.dumps(payload).encode()).decode(),
            }
        )


@pytest.fixture(params=["e2b", "docker"])
def gateway(request):
    requests = _Requests()
    if request.param == "e2b":
        pytest.importorskip("e2b", reason="E2B backend requires the optional e2b SDK")
        # Exercise the real E2BBackend, including its SDK default when omitted.
        async def run(command, *, cwd, timeout=60):
            stdout = await requests.execute(shlex.split(command), timeout)
            return SimpleNamespace(exit_code=0, stdout=stdout, stderr="")

        backend = E2BBackend(
            SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(side_effect=run))),
            "/workspace",
        )
    else:
        backend = DockerBackend(SimpleNamespace(), "/workspace")

        async def exec_shell(argv, *, timeout=None):
            stdout = await requests.execute(argv, timeout)
            return ExecResult(exit_code=0, stdout=stdout.encode(), stderr=b"")

        backend.exec_shell = AsyncMock(side_effect=exec_shell)
    backend.write_file = AsyncMock()
    backend.delete_path = AsyncMock()
    gateway = GatewayClient(
        backend,
        gateway_port=8080,
        timeout=30.0,
        auth_token="gateway-token",
        instance_nonce="gateway-instance",
    )
    client = gateway.make_client(
        {
            "name": "context-manager",
            "is_stateful": False,
            "mcp_config": {"type": "http_mcp", "url": "http://cm.test/mcp"},
        },
        agent_id="agent",
        session_id="session",
        connected=True,
    )
    return SimpleNamespace(
        gateway=gateway, client=client, requests=requests, backend_kind=request.param
    )


def _context(client, *, seconds=1800, enabled=True):
    return SimpleNamespace(
        workspace=SimpleNamespace(
            list_mcps=AsyncMock(return_value=[client]),
            list_tools=AsyncMock(return_value=[]),
            list_skills=AsyncMock(return_value=[]),
        ),
        paths=SimpleNamespace(agent_id="agent"),
        session_id="session",
        disabled_skills=frozenset(),
        user_runtime_config=SimpleNamespace(
            tool_timeout_enabled=enabled, tool_timeout_seconds=seconds
        ),
    )


async def _tool(context):
    toolkit = await DataAgentBuilder().build_toolkit(run_context=context)
    tool = await toolkit.get_tool("mcp__context-manager__search_context")
    assert tool is not None
    return tool


@pytest.mark.parametrize("seconds,enabled", [(1800, True), (90, True), (90, False)])
async def test_builder_binds_page_timeout_and_preserves_health(
    gateway, seconds, enabled
):
    h = gateway
    tool = await _tool(_context(h.client, seconds=seconds, enabled=enabled))
    assert (await tool(query="最近一周游戏运营情况", domain="game")).state == (
        ToolResultState.SUCCESS
    )
    assert await tool._gateway.health()
    assert await h.gateway.health()

    expected = seconds if enabled else (0.0 if h.backend_kind == "e2b" else None)
    assert [
        (method, path, timeout) for method, path, timeout, _, _ in h.requests.calls
    ] == [
        ("GET", "/mcps/context-manager/tools", expected),
        ("POST", "/mcps/context-manager/tools/search_context", expected),
        ("GET", "/health", 30.0),
        ("GET", "/health", 30.0),
    ]
    assert h.requests.calls[1][3:] == (
        {"agent_id": ["agent"], "session_id": ["session"]},
        "gateway-token",
    )
    assert h.requests.calls[-2][-1] == ""  # Health probes must not send the token.
    assert h.client._gateway is h.gateway
    assert h.gateway.timeout == 30.0
    assert h.client.is_connected


async def test_concurrent_runs_and_next_turn_do_not_overwrite_timeouts(gateway):
    h = gateway
    old_tool = await _tool(_context(h.client, seconds=90))
    next_tool = await _tool(_context(h.client, seconds=1800))
    disabled_tool = await _tool(_context(h.client, enabled=False))
    h.requests.calls.clear()
    h.requests.release.clear()

    tasks = [asyncio.create_task(tool(query="q")) for tool in (old_tool, next_tool)]
    try:
        await asyncio.wait_for(h.requests.started.wait(), 1)
        assert await old_tool._gateway.health()
        assert await next_tool._gateway.health()
        h.requests.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        await disabled_tool(query="q")
        await old_tool(query="q")
    finally:
        h.requests.release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    no_limit = 0.0 if h.backend_kind == "e2b" else None
    assert [call[2] for call in h.requests.calls if call[0] == "POST"] == [
        90,
        1800,
        no_limit,
        90,
    ]
    assert all(call[2] == 30.0 for call in h.requests.calls if call[1] == "/health")
    assert h.gateway.timeout == 30.0


async def test_failure_diagnostic_keeps_health_timeout(gateway):
    h = gateway
    tool = await _tool(_context(h.client, enabled=False))
    h.requests.calls.clear()
    h.requests.fail_tool = True

    with pytest.raises(RuntimeError, match="context deadline exceeded"):
        await tool(query="q")

    assert h.requests.calls[-1][:3] == ("GET", "/health", 30.0)
    assert h.gateway.timeout == 30.0


async def test_page_timeout_still_cancels_the_tool_call(gateway):
    h = gateway
    context = _context(h.client, seconds=0.05)
    tool = await _tool(context)
    h.requests.release.clear()

    async def next_handler(**kwargs):
        yield await tool(query="q")

    items = [
        item
        async for item in ToolTimeoutMiddleware(run_context=context).on_acting(
            SimpleNamespace(), {"tool_call": tool}, next_handler
        )
    ]
    assert h.requests.cancelled.is_set()
    assert len(items) == 1
    assert items[0].state == ToolResultState.ERROR
    error = json.loads(items[0].content[0].text)["error"]
    assert error["code"] == "TOOL_TIMEOUT"
    assert error["timeout_seconds"] == 0.05


def test_local_mcp_keeps_its_transport():
    client = MCPClient(
        name="local",
        is_stateful=False,
        mcp_config={"type": "http_mcp", "url": "http://cm.test/mcp"},
    )
    assert bind_mcp_tool_timeout(client, 1800) is client
