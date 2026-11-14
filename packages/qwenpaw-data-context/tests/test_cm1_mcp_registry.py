from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from qwenpaw_data_auth.authorization import AuthorizationContext

from context_manager.api.auth import ALL_SCOPES, AuthPrincipal
from context_manager.mcp import cm_server
from context_manager.mcp.legacy import LEGACY_TOOL_NAMES
from context_manager.mcp.server import (
    MCP_PATH, NATIVE_TOOL_NAMES, TOOL_SCOPE_POLICIES, create_mcp_server,
    mount_mcp, resolve_mcp_context,
)


@pytest.mark.asyncio
async def test_native_and_legacy_inventories_share_one_server():
    server = create_mcp_server(FastAPI())
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 27
    assert set(tools) == LEGACY_TOOL_NAMES | NATIVE_TOOL_NAMES
    assert not LEGACY_TOOL_NAMES & NATIVE_TOOL_NAMES
    assert set(TOOL_SCOPE_POLICIES) == set(tools)
    for name in tools:
        assert "metadata" not in tools[name].inputSchema.get("properties", {})
        assert "session_ref" not in tools[name].inputSchema.get("properties", {})
    for name in NATIVE_TOOL_NAMES:
        assert tools[name].annotations.readOnlyHint == (name not in {"execute_create", "execute_modify"})


@pytest.mark.asyncio
async def test_all_20_legacy_input_schemas_match_public_03():
    baseline = {tool.name: tool.inputSchema for tool in await cm_server.mcp.list_tools()}
    current = {tool.name: tool.inputSchema for tool in await create_mcp_server(FastAPI()).list_tools()}
    assert set(baseline) == LEGACY_TOOL_NAMES
    assert {name: current[name] for name in LEGACY_TOOL_NAMES} == baseline


@pytest.mark.asyncio
async def test_native_schemas_bound_resources_and_exclude_private_sql_modes():
    tools = {tool.name: tool for tool in await create_mcp_server(FastAPI()).list_tools()}
    for name in {"search_graph_fulltext", "search_graph_vector"}:
        properties = tools[name].inputSchema["properties"]
        assert properties["limit"]["minimum"] == 1
        assert properties["limit"]["maximum"] == 100
        assert properties["text"]["maxLength"] == 8000
    for name in {"execute_read", "execute_create", "execute_modify"}:
        properties = tools[name].inputSchema["properties"]
        assert properties["sql"]["maxLength"] == 50000
        assert properties["max_rows"]["maximum"] == 10000
        assert properties["timeout_ms"]["maximum"] == 30000
        assert "odps_execution_project" not in properties


def _context(*, scopes=ALL_SCOPES, role="member", authorization=True):
    request = Request({
        "type": "http", "scheme": "http", "path": MCP_PATH, "query_string": b"",
        "server": ("localhost", 80), "client": ("127.0.0.1", 50000), "headers": [],
    })
    request.state.auth_principal = AuthPrincipal("u1", scopes, "key-1")
    if authorization:
        request.state.cm_authorization = AuthorizationContext(
            "u1", "t1", role, role, frozenset({"w1"}),
            frozenset({"w1"}) if role == "ws_admin" else frozenset(), "w1",
        )
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


@pytest.mark.parametrize("tool_name", ["execute_create", "execute_modify"])
def test_read_keys_never_gain_sql_write_authority(tool_name):
    with pytest.raises(ToolError, match="scope"):
        resolve_mcp_context(FastAPI(), _context(scopes=frozenset({"query"}), role="ws_admin"), tool_name)


@pytest.mark.parametrize("tool_name", ["execute_create", "execute_modify"])
def test_sql_write_grant_is_intersected_with_current_workspace_role(tool_name):
    with pytest.raises(ToolError, match="workspace administration"):
        resolve_mcp_context(FastAPI(), _context(), tool_name)


@pytest.mark.parametrize("tool_name", ["query_graph", "search_context", "get_metric"])
def test_capability_alone_never_substitutes_for_rbac_identity(tool_name):
    with pytest.raises(ToolError, match="authenticated HTTP"):
        resolve_mcp_context(FastAPI(), _context(authorization=False), tool_name)


def test_unknown_tool_names_fail_closed():
    with pytest.raises(ToolError, match="scope"):
        resolve_mcp_context(FastAPI(), _context(role="ws_admin"), "unclassified_tool")


def test_each_application_has_independent_mcp_registration():
    first, second = FastAPI(), FastAPI()
    assert mount_mcp(first) is not mount_mcp(second)
    assert first.state.context_mcp._tool_manager is not second.state.context_mcp._tool_manager
    with pytest.raises(RuntimeError, match="already mounted"):
        mount_mcp(first)
    assert [route.path for route in first.routes].count(MCP_PATH) == 1


def test_streamable_transport_lists_both_inventories_and_denies_unbound_calls(tmp_path, monkeypatch):
    from context_manager.mcp import harness_tool

    monkeypatch.setattr(harness_tool, "_MCP_LOG_PATH_STR", str(tmp_path / "mcp-access.log"))

    @asynccontextmanager
    async def lifespan(app):
        async with app.state.context_mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    mount_mcp(app)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        response = client.post(MCP_PATH, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert response.status_code == 200
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == LEGACY_TOOL_NAMES | NATIVE_TOOL_NAMES
        for tool_name, arguments in (("get_graph_schema", {}), ("list_domains", {})):
            result = client.post(MCP_PATH, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            assert result.status_code == 200
            assert result.json()["result"]["isError"] is True
            assert "scope" in json.dumps(result.json())
