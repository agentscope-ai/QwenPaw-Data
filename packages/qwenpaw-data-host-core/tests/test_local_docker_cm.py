"""OSS Docker keeps its builtin local CM in the Host, not the workspace gateway."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.workspace import DockerWorkspace, LocalWorkspace

from qwenpaw_data.host.core.integrations.cm.local_mcp import LocalCMClient, use_host_cm
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.utils.mcp import bind_mcp_tool_timeout


@pytest.fixture
def local_cm(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:18769")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "local")
    clear_settings()
    yield
    clear_settings()


@pytest.mark.parametrize(
    "mode,workspace,url,hosted",
    [
        ("local", DockerWorkspace, "http://localhost:8765", True),
        ("local", DockerWorkspace, "http://[::1]:8765", True),
        ("multi-user", DockerWorkspace, "http://127.0.0.1:8765", False),
        ("local", DockerWorkspace, "https://cm.example", False),
        ("local", LocalWorkspace, "http://localhost:8765", False),
    ],
)
def test_route_boundary(local_cm, monkeypatch, mode, workspace, url, hosted):
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", mode)
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", url)
    clear_settings()
    assert use_host_cm(Mock(spec=workspace)) is hosted


@pytest.mark.parametrize("status", [200, 401, 403])
async def test_turn_routes_only_builtin_and_preserves_refusals(
    local_cm, monkeypatch, tmp_path, status
):
    requests = []

    def respond(request):
        data = json.loads(request.content)
        requests.append(request)
        method = data["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": data["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cm", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": n, "inputSchema": {"type": "object"}}
                    for n in ("execute_read", "execute_modify")
                ]
            }
        else:
            assert method == "tools/call" and data["params"]["name"] == "execute_read"
            if status != 200:
                detail = (
                    "Local access requires a safe loopback client"
                    if status == 403
                    else "private diagnostic"
                )
                return httpx.Response(status, json={"detail": detail})
            result = {
                "content": [{"type": "text", "text": "one row"}],
                "isError": False,
            }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": data["id"], "result": result}
        )

    client_cls = httpx.AsyncClient

    def connect(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client_cls(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", connect)
    repo = McpRepository(tmp_path / "mcp.json")
    previous = repo.require("context-manager").to_client()
    workspace = Mock(spec=DockerWorkspace)
    workspace.list_mcps = AsyncMock(return_value=[previous])
    workspace.remove_mcp = AsyncMock()
    workspace.add_mcp = AsyncMock()
    kwargs = dict(agent_id="a", session_id="s", request_id="c", mcp_headers={})
    cm = await repo.sync(
        workspace, **kwargs, datasource_id="demo", access_token="only-this-turn"
    )
    assert isinstance(cm, LocalCMClient)
    workspace.remove_mcp.assert_awaited_once_with(
        "context-manager", agent_id="a", session_id="s"
    )
    workspace.add_mcp.assert_not_awaited()
    assert "only-this-turn" not in repo.manifest_path.read_text()
    other = SimpleNamespace(name="other")
    workspace.list_mcps.return_value = [other]
    ctx = RunContext(
        "s", "c", workspace, SimpleNamespace(agent_id="a"), None, None, None, host_cm=cm
    )
    assert await ctx.list_mcps() == [other, cm]
    assert [tool.name for tool in await cm.list_raw_tools()] == ["execute_read"]
    assert bind_mcp_tool_timeout(cm, 17) is cm and cm.execution_timeout == 17
    toolkit = Toolkit(mcps=[cm])
    chunks = [
        item
        async for item in toolkit.call_tool(
            ToolCallBlock(
                id="test",
                name="mcp__context-manager__execute_read",
                input='{"sql":"SELECT 1"}',
            ),
            AgentState(),
        )
    ]
    outcome = chunks[-1]
    if status == 200:
        assert outcome.content[0].text == "one row"
    else:
        assert outcome.state == ToolResultState.ERROR
        assert f"DataBridge HTTP {status}" in outcome.content[0].text
        assert "private diagnostic" not in outcome.content[0].text
        if status == 403:
            assert "safe loopback client" in outcome.content[0].text
    assert all(
        req.headers["authorization"] == "Bearer only-this-turn"
        and req.headers["datasource_id"] == "demo"
        and req.url.host == "127.0.0.1"
        for req in requests
    )
    repo.disable("context-manager")
    workspace.list_mcps.return_value = []
    assert await repo.sync(workspace, **kwargs) is None
