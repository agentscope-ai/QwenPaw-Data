# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Annotated, Any, Literal

from agentscope.mcp import HttpMCPConfig, StdioMCPConfig
from pydantic import Field

from qwenpaw_data.host.core.api.models.common import ApiModel

McpConfigSchema = Annotated[
    StdioMCPConfig | HttpMCPConfig,
    Field(discriminator="type"),
]


class McpWriteRequest(ApiModel):
    name: str
    description: str = ""
    enabled: bool = True
    source: Literal["builtin", "custom"] = "custom"
    is_stateful: bool
    mcp_config: McpConfigSchema
    enable_tools: list[str] | None = None
    disable_tools: list[str] | None = None
    execution_timeout: float | None = None


class McpToolsUpdateRequest(ApiModel):
    enable_tools: list[str] | None = None
    disable_tools: list[str] | None = None


class McpSchema(ApiModel):
    name: str
    enabled: bool
    source: str
    description: str = ""
    is_stateful: bool
    mcp_config: dict[str, Any]
    enable_tools: list[str] | None = None
    disable_tools: list[str] | None = None
    execution_timeout: float | None = None


class McpToolSchema(ApiModel):
    name: str
    description: str = ""
    enabled: bool
    input_schema: dict[str, Any]
