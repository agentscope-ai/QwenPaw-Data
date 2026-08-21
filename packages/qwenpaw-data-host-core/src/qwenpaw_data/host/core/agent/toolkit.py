# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from agentscope.tool import Toolkit, ToolGroup

from ..constants import is_spawn_subagent_enabled
from ..mcp_cm import is_cm_mcp_config, prepare_cm_mcp_clients
from ..orchestration import RuntimeStateManager
from ..orchestration.tools import PLAN_MODE_TOOL_NAMES, build_qwenpaw_data_tools
from .mcp_client_log import MCP_CLIENT_RUN_ID, MCP_RUN_HEADER, log_mcp_client_event
from .spawn_subagent import SpawnSubagent

logger = logging.getLogger(__name__)

_PLAN_MODE_BLOCKED_MCP_TOOL_NAMES = {"execute_sql"}

# Fallback execution timeout (seconds) for MCP clients without one.
# AgentScope defaults ``execution_timeout`` to None, which means
# ``session.call_tool(read_timeout_seconds=None)`` waits forever when the
# server crashes without sending a JSON-RPC response. Override via
# ``QWENPAW_DATA_MCP_EXECUTION_TIMEOUT`` (<= 0 disables the fallback).
DEFAULT_MCP_EXECUTION_TIMEOUT = 120.0


def _default_mcp_execution_timeout() -> float:
    raw = os.environ.get("QWENPAW_DATA_MCP_EXECUTION_TIMEOUT", "")
    try:
        return float(raw) if raw.strip() else DEFAULT_MCP_EXECUTION_TIMEOUT
    except ValueError:
        return DEFAULT_MCP_EXECUTION_TIMEOUT


def apply_default_mcp_execution_timeouts(mcps: list[Any]) -> None:
    """Give every MCP client a finite execution timeout as a safety net."""
    default_timeout = _default_mcp_execution_timeout()
    if default_timeout <= 0:
        return
    for client in mcps:
        if getattr(client, "execution_timeout", None) is None:
            try:
                client.execution_timeout = default_timeout
            except Exception:
                logger.warning(
                    "Failed to set default execution_timeout on MCP client %r",
                    getattr(client, "name", repr(client)),
                    exc_info=True,
                )


def _mcp_raw_tool_name(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1]


def _is_plan_safe_mcp_tool(tool_name: str) -> bool:
    return _mcp_raw_tool_name(tool_name) not in _PLAN_MODE_BLOCKED_MCP_TOOL_NAMES


def _is_agent_only_mcp_tool(tool_name: str) -> bool:
    raw_name = tool_name.rsplit("__", 1)[-1]
    return raw_name in _PLAN_MODE_BLOCKED_MCP_TOOL_NAMES


# Discovery (tools/list) timeout in seconds; override via env.
DEFAULT_MCP_DISCOVERY_TIMEOUT = 15.0


def _mcp_discovery_timeout() -> float:
    raw = os.environ.get("QWENPAW_DATA_MCP_DISCOVERY_TIMEOUT", "")
    try:
        return float(raw) if raw.strip() else DEFAULT_MCP_DISCOVERY_TIMEOUT
    except ValueError:
        return DEFAULT_MCP_DISCOVERY_TIMEOUT


class _MCPToolCatalog:
    """One shared tools/list discovery per underlying MCP client.

    Both the plan and agent ``_FilteredMCPClient`` views (and sub-agent
    toolkits) reuse this catalog, so the tool list is fetched from the MCP
    server exactly once per process instead of once per reasoning round per
    view. Discovery is bounded by a timeout so a silent server cannot hang
    the agent during schema collection.
    """

    def __init__(self, client: Any, timeout: float | None = None) -> None:
        self._client = client
        self._timeout = timeout if timeout is not None else _mcp_discovery_timeout()
        self._tools: list[Any] | None = None
        self._lock = asyncio.Lock()

    async def list_tools(self) -> list[Any]:
        if self._tools is not None:
            return self._tools
        async with self._lock:
            if self._tools is not None:
                return self._tools
            name = getattr(self._client, "name", repr(self._client))
            log_mcp_client_event("MCP_DISCOVERY_START client=%s", name)
            t0 = time.monotonic()
            try:
                async with asyncio.timeout(self._timeout):
                    tools = await self._client.list_tools()
            except BaseException as exc:
                log_mcp_client_event(
                    "MCP_DISCOVERY_END client=%s %dms error=%s: %s",
                    name,
                    int((time.monotonic() - t0) * 1000),
                    type(exc).__name__,
                    exc,
                )
                raise
            self._tools = list(tools)
            log_mcp_client_event(
                "MCP_DISCOVERY_END client=%s %dms tools=%d",
                name,
                int((time.monotonic() - t0) * 1000),
                len(self._tools),
            )
            return self._tools


class _FilteredMCPClient:
    """Expose a filtered MCP tool set from a shared catalog."""

    def __init__(self, client: Any, catalog: _MCPToolCatalog, predicate: Any) -> None:
        self._client = client
        self._catalog = catalog
        self._predicate = predicate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def list_tools(self) -> list[Any]:
        tools = await self._catalog.list_tools()
        return [
            tool
            for tool in tools
            if self._predicate(getattr(tool, "name", ""))
        ]


def _filtered_mcps(
    mcps: list[Any],
    catalogs: dict[int, _MCPToolCatalog],
    predicate: Any,
) -> list[Any]:
    return [
        _FilteredMCPClient(client, catalogs[id(client)], predicate)
        for client in mcps
    ]


def _inject_run_header(mcps: list[Any]) -> None:
    """Add runtime-only correlation and CM authentication headers.

    DataBridge's protocol log records the ``x-qwenpaw-data-run`` header, so lines
    in ``mcp_client.log`` (CLI) and ``mcp_access.log`` (CM) can be joined.
    The API token is injected only for the recognized CM endpoint and is never
    persisted into the workspace's ``.mcp`` file.
    """
    api_token = (
        (os.environ.get("QWENPAW_DATA_CLIENT_API_TOKEN") or "").strip()
        or (os.environ.get("QWENPAW_DATA_API_TOKEN") or "").strip()
    )
    for client in mcps:
        mcp_config = getattr(client, "mcp_config", None)
        if getattr(mcp_config, "type", None) != "http_mcp":
            continue
        try:
            headers = getattr(mcp_config, "headers", None)
            if headers is None:
                mcp_config.headers = {MCP_RUN_HEADER: MCP_CLIENT_RUN_ID}
                headers = mcp_config.headers
            else:
                headers[MCP_RUN_HEADER] = MCP_CLIENT_RUN_ID
            if api_token and is_cm_mcp_config(mcp_config):
                headers.setdefault("Authorization", f"Bearer {api_token}")
        except Exception:
            logger.debug(
                "failed to inject run header on MCP client %r",
                getattr(client, "name", repr(client)),
                exc_info=True,
            )


class QwenPawDataToolkit(Toolkit):
    """Toolkit with QwenPaw Data's externally controlled mode boundary."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._qwenpaw_data_mode: str | None = None

    def set_qwenpaw_data_mode(self, mode: str) -> None:
        self._qwenpaw_data_mode = mode if mode in {"plan", "agent"} else None

    async def _get_available_tools(self, groups: list[str] | None) -> dict:
        effective_groups = (
            [self._qwenpaw_data_mode]
            if self._qwenpaw_data_mode in {"plan", "agent"}
            else groups
        )
        tools = await super()._get_available_tools(effective_groups)
        tools.pop(self.builtin_meta_tool.tool.name, None)
        return tools


async def build_qwenpaw_data_toolkit(
    runtime_state: RuntimeStateManager,
    *,
    workspace: Any,
    parent_agent_getter: Any | None = None,
    workspace_dir: Any | None = None,
    artifacts_root: Any | None = None,
    session_id_getter: Any | None = None,
    request_context_getter: Any | None = None,
) -> Toolkit:
    """拼装 QwenPaw Data Agent 用的 grouped ``Toolkit``。"""
    shared_tools = list(build_qwenpaw_data_tools(runtime_state, mode="plan"))
    agent_only_tools = [
        tool
        for tool in build_qwenpaw_data_tools(runtime_state, mode="agent")
        if tool.name not in PLAN_MODE_TOOL_NAMES
    ]
    workspace_tools = list(await workspace.list_tools())
    workspace_mcps = list(await workspace.list_mcps())
    # Filters URL-confirmed CM MCP clients, raises their timeouts, and records
    # the AgentScope-generated tool-name prefixes used later for call matching.
    cm_mcp_tool_prefixes = prepare_cm_mcp_clients(workspace_mcps)
    # Any client still without an execution timeout gets a finite fallback so
    # a silent server-side failure surfaces as a timeout instead of a hang.
    apply_default_mcp_execution_timeouts(workspace_mcps)
    # Tag HTTP clients with this process's run id (x-qwenpaw-data-run) so the
    # CM-side protocol log can be joined with the client-side MCP log.
    _inject_run_header(workspace_mcps)
    # One shared discovery catalog per client: plan/agent views and sub-agent
    # toolkits reuse it, so tools/list hits the server once per process.
    mcp_catalogs = {
        id(client): _MCPToolCatalog(client) for client in workspace_mcps
    }
    subagent_mcps = _filtered_mcps(
        workspace_mcps,
        mcp_catalogs,
        lambda _name: True,
    )
    workspace_skills = list(await workspace.list_skills())
    if is_spawn_subagent_enabled():
        agent_only_tools.append(
            SpawnSubagent(
                runtime_state=runtime_state,
                workspace=workspace,
                workspace_tools=workspace_tools,
                workspace_mcps=subagent_mcps,
                workspace_skills=workspace_skills,
                parent_agent_getter=parent_agent_getter,
                workspace_dir=workspace_dir or getattr(workspace, "workdir", None),
                artifacts_root=artifacts_root,
                session_id_getter=session_id_getter,
                request_context_getter=request_context_getter,
                cm_mcp_tool_prefixes=cm_mcp_tool_prefixes,
            ),
        )
    toolkit = QwenPawDataToolkit(
        # AgentScope always enables the basic group, so keep only tools that are
        # valid in both Plan Mode and Agent Mode here.
        tools=shared_tools,
        mcps=_filtered_mcps(workspace_mcps, mcp_catalogs, _is_plan_safe_mcp_tool),
        tool_groups=[
            ToolGroup(
                name="plan",
                description="Tools for creating and revising a QwenPaw Data plan.",
                instructions=(
                    "Use these tools only to create or revise the DAG. "
                    "You may use MCP metadata/read tools to clarify the plan, "
                    "but SQL execution tools such as execute_sql are not "
                    "available in planning mode."
                ),
            ),
            ToolGroup(
                name="agent",
                description="Tools for executing a QwenPaw Data DAG and producing artifacts.",
                instructions=(
                    "Use update_subtask to record progress for each DAG node. "
                    "Use workspace tools only for node execution and artifact generation."
                ),
                tools=agent_only_tools + workspace_tools,
                mcps=_filtered_mcps(
                    workspace_mcps,
                    mcp_catalogs,
                    _is_agent_only_mcp_tool,
                ),
                skills_or_loaders=workspace_skills,
            ),
        ],
    )
    toolkit._qwenpaw_data_cm_mcp_tool_prefixes = cm_mcp_tool_prefixes
    return toolkit
