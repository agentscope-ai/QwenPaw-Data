# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import ClassVar

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_CM_", extra="ignore")

    DEFAULT_BASE_URL: ClassVar[str] = "http://127.0.0.1:8765"
    # Matches the host frontend default for VITE_CONTEXT_FRONTEND_URL.
    # 同源部署导出 VITE_CONTEXT_FRONTEND_URL=/，不会走到此默认。
    DEFAULT_CM_FRONTEND_URL: ClassVar[str] = "http://localhost:3000"
    MCP_PATH: ClassVar[str] = "/mcp/v1/cm"
    MCP_NAME: ClassVar[str] = "context-manager"

    # Host requests use this address, including MCP inventory and CSV downloads.
    base_url: str | None = None
    sandbox_base_url: str | None = None
    # Entity-link href origin; reads VITE_CONTEXT_FRONTEND_URL (no QWENPAW_DATA_CM_ prefix),
    # then the 0.3 name QWENPAW_DATA_CM_FRONTEND_URL.
    # "/" → rstrip 后为空串，链接形如 /metrics?...；未设 → 本地默认。
    cm_frontend_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VITE_CONTEXT_FRONTEND_URL",
            "QWENPAW_DATA_CM_FRONTEND_URL",
        ),
    )
    mcp_timeout: float = 2400.0

    def client_base_url(self) -> str:
        return (self.base_url or self.DEFAULT_BASE_URL).rstrip("/")

    def client_cm_frontend_url(self) -> str:
        raw = (
            self.DEFAULT_CM_FRONTEND_URL
            if self.cm_frontend_url is None
            else self.cm_frontend_url
        )
        return raw.rstrip("/")

    def mcp_url(self) -> str:
        return f"{self.client_base_url()}{self.MCP_PATH}"

    def sandbox_mcp_url(self) -> str:
        base_url = (self.sandbox_base_url or "").strip().rstrip("/")
        if not base_url:
            raise ValueError(
                "QWENPAW_DATA_CM_SANDBOX_BASE_URL is required for cloud sandbox backends"
            )
        return f"{base_url}{self.MCP_PATH}"
