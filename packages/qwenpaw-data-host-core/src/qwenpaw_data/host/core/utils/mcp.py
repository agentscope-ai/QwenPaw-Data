# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from copy import copy
from pathlib import Path
from typing import Any

from agentscope.mcp import MCPClient
from agentscope.workspace import E2BBackend
from agentscope.workspace._gateway_client import GatewayMCPClient

from qwenpaw_data.host.core.utils.workspace import write_json_atomic

_BUILTIN_MCP_PATH = Path(__file__).resolve().parent.parent / "data" / "builtin_mcp.json"


def bind_mcp_tool_timeout(client: MCPClient, timeout: float | None) -> MCPClient:
    """Bind the MCP timeout (None disables it), preserving the original health check."""
    if not isinstance(client, GatewayMCPClient):
        return client
    assert client._gateway is not None
    bound = client.model_copy()
    gateway = copy(client._gateway)
    gateway.timeout = timeout
    # E2B defaults to 60s for None; Docker requires None to disable its timer.
    if gateway.timeout is None and isinstance(gateway.backend, E2BBackend):
        gateway.timeout = 0.0
    # This bound method keeps /health at the workspace's 30s timeout, including
    # the SDK's automatic health probe after a failed MCP request.
    gateway.health = client._gateway.health
    bound._gateway = gateway
    return bound


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "****" + value[-4:] if len(value) > 8 else "****"


def mask_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("headers", "env"):
        values = config.get(key)
        if isinstance(values, dict):
            config[key] = {name: _mask(value) for name, value in values.items()}
    return config


def read_mcp_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"mcp manifest must be a JSON object: {path}")
    records = payload.get("mcps")
    if not isinstance(records, dict):
        raise ValueError(f"invalid mcp manifest mcps map: {path}")
    for name, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"invalid mcp record: {name}")
    return records


def load_builtin_mcp_records() -> dict[str, dict[str, Any]]:
    records = read_mcp_manifest(_BUILTIN_MCP_PATH)
    if not records:
        raise FileNotFoundError(f"builtin MCP preset is empty: {_BUILTIN_MCP_PATH}")
    return records


def _strip_authorization(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    config = item.get("mcp_config")
    if not isinstance(config, dict):
        return item
    headers = config.get("headers")
    if not isinstance(headers, dict):
        return item
    item["mcp_config"] = {
        **config,
        "headers": {
            key: value
            for key, value in headers.items()
            if str(key).lower() != "authorization"
        },
    }
    return item


def write_mcp_manifest(path: Path, records: dict[str, dict[str, Any]]) -> None:
    cleaned = {name: _strip_authorization(record) for name, record in records.items()}
    # Unique fsynced temp + os.replace (Windows sharing-violation retry): two
    # writers never share one ``mcp.json.tmp`` and readers never see a torn file.
    write_json_atomic(Path(path), {"mcps": cleaned})


def public_tool(
    tool: Any,
    *,
    enable_tools: list[str] | None,
    disable_tools: list[str] | None,
) -> dict[str, Any]:
    schema = (
        getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    )
    if not isinstance(schema, dict):
        raise ValueError(
            f"MCP tool input schema must be an object: {getattr(tool, 'name', tool)}",
        )
    allow = None if enable_tools is None else set(enable_tools)
    deny = set(disable_tools or ())
    return {
        "name": str(tool.name),
        "description": str(getattr(tool, "description", "") or ""),
        "enabled": (allow is None or tool.name in allow) and tool.name not in deny,
        "input_schema": schema,
    }
