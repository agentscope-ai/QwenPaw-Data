# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_MCP_", extra="ignore")

    HEADERS_HEADER: ClassVar[str] = "X-QwenPaw-Data-Mcp-Headers"
    probe_timeout: float = Field(default=10.0, gt=0)
    # Binds QWENPAW_DATA_MCP_REGISTRATION_HEADERS (set by the gateway in holo
    # deployment envs): headers registered with every MCP by default, keyed by
    # MCP name then header name. Same shape as the X-QwenPaw-Data-Mcp-Headers request
    # overlay, which wins on a name collision. The value here reaches sandboxes
    # that never pass a gateway entry (cron, IM, run_now); it is written into the
    # registration config, so keep it to routing values and never a secret.
    registration_headers: dict[str, dict[str, str]] = Field(default_factory=dict)
    # A stdio MCP runs a configured command as a local process of whoever
    # connects it -- in hosted deployments that is the tenant pod's host
    # process holding every platform secret. Default-off; local development
    # opts in with QWENPAW_DATA_MCP_ALLOW_CUSTOM_STDIO=1.
    allow_custom_stdio: bool = False
