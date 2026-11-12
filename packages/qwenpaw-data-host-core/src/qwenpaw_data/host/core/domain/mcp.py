# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agentscope.mcp import MCPClient

from qwenpaw_data.host.core.utils.mcp import mask_mcp_config
from qwenpaw_data.host.core.utils.safe_name import require_safe_name

McpSource = Literal["builtin", "custom"]


class McpError(Exception):
    """Base error for MCP domain operations."""


class McpConflictError(RuntimeError, McpError):
    def __init__(self, message: str) -> None:
        super().__init__(f"CONFLICT: {message}")


class McpNotFoundError(LookupError, McpError):
    """MCP is missing from the inventory."""


class McpConnectFailed(McpError):
    """CM MCP is unreachable."""


@dataclass(frozen=True)
class Mcp:
    client: MCPClient
    enabled: bool
    source: McpSource
    description: str = ""

    @property
    def name(self) -> str:
        return self.client.name

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> Mcp:
        if not isinstance(data, dict):
            raise ValueError("mcp record must be an object")
        name = require_safe_name(str(data.get("name") or ""))
        if "enabled" not in data:
            raise ValueError(f"mcp enabled is required: {name}")
        source = data.get("source")
        if source not in ("builtin", "custom"):
            raise ValueError(f"invalid MCP source: {name}")
        payload = {
            key: value
            for key, value in data.items()
            if key not in {"enabled", "source", "description"}
        }
        payload["name"] = name
        return cls(
            client=MCPClient.model_validate(payload),
            enabled=bool(data["enabled"]),
            source=source,
            description=str(data.get("description") or ""),
        )

    def to_client(self) -> MCPClient:
        config = self.client.mcp_config
        if config.type == "http_mcp" and not config.url.strip():
            raise ValueError("HTTP MCP url is required")
        if config.type == "stdio_mcp" and not config.command.strip():
            raise ValueError("STDIO MCP command is required")
        return self.client

    def to_record(self) -> dict[str, Any]:
        payload = self.to_client().model_dump(mode="json")
        payload.update(
            enabled=self.enabled,
            source=self.source,
            description=self.description,
        )
        return payload

    def public_record(self) -> dict[str, Any]:
        payload = self.to_record()
        mask_mcp_config(payload["mcp_config"])
        return payload
