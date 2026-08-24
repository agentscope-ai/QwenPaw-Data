# -*- coding: utf-8 -*-
"""Context Manager MCP helpers."""
from __future__ import annotations

import json
import os
from typing import Any, Iterable

_DEFAULT_CM_MCP_READ_TIMEOUT = 2400.0


def _resolve_cm_mcp_read_timeout() -> float:
    """CM MCP read/execution timeout, overridable via env.

    Long CM SQL tasks may legitimately run for a long time, but 2400s also
    means a server-side silent failure blocks the agent for 40 minutes.
    Tune with ``QWENPAW_DATA_CM_MCP_TIMEOUT`` (seconds).
    """
    raw = os.environ.get("QWENPAW_DATA_CM_MCP_TIMEOUT", "")
    try:
        value = float(raw) if raw.strip() else _DEFAULT_CM_MCP_READ_TIMEOUT
    except ValueError:
        return _DEFAULT_CM_MCP_READ_TIMEOUT
    return value if value > 0 else _DEFAULT_CM_MCP_READ_TIMEOUT


CM_MCP_READ_TIMEOUT = _resolve_cm_mcp_read_timeout()
CM_MCP_URL_MARKER = "/mcp/v1/cm"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set_field(obj: Any, name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _raised_timeout(value: Any) -> float:
    try:
        if value is not None and float(value) >= CM_MCP_READ_TIMEOUT:
            return float(value)
    except (TypeError, ValueError):
        pass
    return CM_MCP_READ_TIMEOUT


def is_cm_mcp_config(mcp_config: Any) -> bool:
    """Return True only for the Context Manager HTTP MCP URL."""
    if _field(mcp_config, "type") != "http_mcp":
        return False
    url = str(_field(mcp_config, "url", "") or "").lower()
    return CM_MCP_URL_MARKER in url


def is_cm_mcp_client(client: Any) -> bool:
    """Return True when an AgentScope MCPClient points at CM."""
    return is_cm_mcp_config(_field(client, "mcp_config"))


def apply_cm_mcp_long_timeouts(client: Any) -> bool:
    """Raise CM MCP HTTP and tool execution timeouts in place."""
    if not is_cm_mcp_client(client):
        return False

    mcp_config = _field(client, "mcp_config")
    _set_field(
        mcp_config,
        "timeout",
        _raised_timeout(_field(mcp_config, "timeout")),
    )
    _set_field(
        client,
        "execution_timeout",
        _raised_timeout(_field(client, "execution_timeout")),
    )
    return True


def cm_mcp_tool_prefix(client: Any) -> str:
    """Return the AgentScope MCP tool-name prefix for a client."""
    return f"mcp__{_field(client, 'name', '')}__"


def prepare_cm_mcp_clients(clients: Iterable[Any]) -> set[str]:
    """Apply CM timeouts and return CM MCP tool-name prefixes.

    This iterates over every MCP client only to find URL-confirmed CM clients.
    Non-CM clients are left untouched. The returned prefixes do not rename
    tools; they mirror AgentScope's MCP tool naming rule:
    ``mcp__<client.name>__<tool.name>``.
    """
    prefixes: set[str] = set()
    for client in clients:
        if apply_cm_mcp_long_timeouts(client):
            prefixes.add(cm_mcp_tool_prefix(client))
    return prefixes


def is_cm_mcp_tool_name(tool_name: str, prefixes: Iterable[str]) -> bool:
    """Return True when a tool name belongs to a CM MCP client."""
    return any(tool_name.startswith(prefix) for prefix in prefixes)


def inject_datasource_metadata(
    tool_call: Any,
    *,
    request_context: dict[str, Any],
    cm_mcp_tool_prefixes: Iterable[str],
) -> None:
    """Inject request datasource metadata into CM MCP tool calls in place."""
    tool_name = getattr(tool_call, "name", "")
    if not tool_name or not is_cm_mcp_tool_name(
        tool_name,
        cm_mcp_tool_prefixes,
    ):
        return

    datasource_id = request_context.get("datasource_id")
    if not datasource_id:
        return

    raw_input = getattr(tool_call, "input", "")
    tool_input: dict[str, Any] = {}
    if isinstance(raw_input, str) and raw_input.strip():
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            tool_input = parsed
    elif isinstance(raw_input, dict):
        tool_input = dict(raw_input)

    tool_input["metadata"] = {"datasource_id": datasource_id}
    tool_call.input = json.dumps(tool_input, ensure_ascii=False)
