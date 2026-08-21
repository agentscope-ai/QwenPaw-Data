from __future__ import annotations

import logging

from agentscope.mcp import MCPClient

from qwenpaw_data.host.core.agent.toolkit import build_qwenpaw_data_toolkit
from qwenpaw_data.host.core.mcp_cm import (
    CM_MCP_READ_TIMEOUT,
    apply_cm_mcp_long_timeouts,
    is_cm_mcp_client,
    prepare_cm_mcp_clients,
)
from qwenpaw_data.host.core.orchestration import RuntimeStateManager

_CM_MCP_URL = "http://localhost:8765/mcp/v1/cm"


class FakeTool:
    description = "fake"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = False
    is_state_injected = False
    is_mcp = True
    mcp_name = "context-manager"

    def __init__(self, name: str) -> None:
        self.name = name


class FakeMCP:
    name = "context-manager"
    is_stateful = False
    is_connected = False
    mcp_config = {
        "type": "http_mcp",
        "url": _CM_MCP_URL,
        "headers": {},
        "timeout": 30.0,
    }
    execution_timeout = None

    async def list_tools(self) -> list[FakeTool]:
        return [
            FakeTool("mcp__context-manager__describe_metrics"),
            FakeTool("mcp__context-manager__execute_sql"),
        ]


def _mcp_client(
    *,
    name: str = "context-manager",
    url: str,
    timeout: float | None = 30.0,
    execution_timeout: float | None = None,
) -> MCPClient:
    return MCPClient(
        name=name,
        is_stateful=False,
        mcp_config={
            "type": "http_mcp",
            "url": url,
            "headers": {},
            "timeout": timeout,
        },
        enable_tools=None,
        disable_tools=None,
        execution_timeout=execution_timeout,
    )


def test_is_cm_mcp_client_by_url() -> None:
    client = _mcp_client(
        url=_CM_MCP_URL,
    )

    assert is_cm_mcp_client(client) is True


def test_is_cm_mcp_client_rejects_same_name_with_other_url() -> None:
    client = _mcp_client(url="http://example.com/mcp")

    assert is_cm_mcp_client(client) is False


def test_apply_cm_mcp_long_timeouts() -> None:
    client = _mcp_client(
        url=_CM_MCP_URL,
    )

    assert apply_cm_mcp_long_timeouts(client) is True

    assert client.mcp_config.timeout == CM_MCP_READ_TIMEOUT
    assert client.execution_timeout == CM_MCP_READ_TIMEOUT


def test_prepare_cm_mcp_clients_collects_tool_prefix() -> None:
    client = _mcp_client(
        url=_CM_MCP_URL,
    )

    prefixes = prepare_cm_mcp_clients([client])

    assert prefixes == {"mcp__context-manager__"}
    assert client.mcp_config.timeout == CM_MCP_READ_TIMEOUT


async def test_build_qwenpaw_data_toolkit_records_cm_mcp_prefix(monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "mcp-runtime-secret")
    client = _mcp_client(
        url=_CM_MCP_URL,
    )

    class FakeWorkspace:
        async def list_tools(self) -> list:
            return [FakeTool("Write"), FakeTool("Bash")]

        async def list_mcps(self) -> list:
            return [client]

        async def list_skills(self) -> list:
            return []

    toolkit = await build_qwenpaw_data_toolkit(
        RuntimeStateManager(),
        workspace=FakeWorkspace(),
    )

    assert toolkit._qwenpaw_data_cm_mcp_tool_prefixes == {"mcp__context-manager__"}
    assert client.execution_timeout == CM_MCP_READ_TIMEOUT
    assert client.mcp_config.headers["Authorization"] == "Bearer mcp-runtime-secret"


async def test_plan_mode_allows_non_sql_mcp_tools_only(caplog, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)
    client = FakeMCP()
    caplog.set_level(logging.WARNING)

    class FakeWorkspace:
        async def list_tools(self) -> list:
            return [FakeTool("Write"), FakeTool("Bash")]

        async def list_mcps(self) -> list:
            return [client]

        async def list_skills(self) -> list:
            return []

    toolkit = await build_qwenpaw_data_toolkit(
        RuntimeStateManager(),
        workspace=FakeWorkspace(),
    )

    toolkit.set_qwenpaw_data_mode("plan")
    plan_names = {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas(["plan"])
    }
    assert "create_plan" in plan_names
    assert "revise_current_plan" in plan_names
    agent_names = {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas(["agent"])
    }
    assert agent_names == plan_names
    assert "reset_tools" not in plan_names

    assert "mcp__context-manager__describe_metrics" in plan_names
    assert "mcp__context-manager__execute_sql" not in plan_names
    assert "spawn_subagent" not in plan_names
    assert "Write" not in plan_names
    assert "Bash" not in plan_names

    toolkit.set_qwenpaw_data_mode("agent")
    agent_names = {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas(["agent"])
    }
    assert "reset_tools" not in agent_names
    assert "create_plan" in agent_names
    assert "revise_current_plan" in agent_names
    assert "mcp__context-manager__execute_sql" in agent_names
    assert "Write" in agent_names
    assert "Bash" in agent_names
    assert "spawn_subagent" in agent_names

    await toolkit.get_tool_schemas(["plan", "agent"])
    await toolkit.get_tool("mcp__context-manager__execute_sql")
    assert "Duplicate tool name" not in caplog.text


async def test_spawn_subagent_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", "0")

    class FakeWorkspace:
        async def list_tools(self) -> list:
            return [FakeTool("Write"), FakeTool("Bash")]

        async def list_mcps(self) -> list:
            return []

        async def list_skills(self) -> list:
            return []

    toolkit = await build_qwenpaw_data_toolkit(
        RuntimeStateManager(),
        workspace=FakeWorkspace(),
    )

    toolkit.set_qwenpaw_data_mode("agent")
    agent_names = {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas(["agent"])
    }
    assert "spawn_subagent" not in agent_names
