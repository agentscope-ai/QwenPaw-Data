"""Turn-local Host transport for the OSS Docker workspace's built-in DataBridge."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from agentscope.mcp import MCPClient
from agentscope.workspace import DockerWorkspace
from mcp.client.streamable_http import streamable_http_client
from qwenpaw_data_auth.config import runtime_mode

from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.settings.cm import CmSettings
from qwenpaw_data.host.core.utils.auth import is_loopback_http_url


def use_host_cm(workspace: Any) -> bool:
    if not isinstance(workspace, DockerWorkspace) or runtime_mode() != "local":
        return False
    url = httpx.URL(get_settings().cm.mcp_url())
    return is_loopback_http_url(url) and not url.query and not url.fragment


class LocalCMClient(MCPClient):
    """Never register this client, or its credentials, in the sandbox."""

    @classmethod
    def from_inventory(cls, spec: MCPClient) -> "LocalCMClient":
        # Only the tool selection comes from the user-writable workspace.
        # The protocol, stateless lifecycle and destination are Host-owned.
        settings = get_settings().cm
        return cls(
            name=CmSettings.MCP_NAME,
            is_stateful=False,
            mcp_config={
                "type": "http_mcp",
                "url": settings.mcp_url(),
                "timeout": settings.mcp_timeout,
            },
            enable_tools=spec.enable_tools,
            disable_tools=spec.disable_tools,
            execution_timeout=settings.mcp_timeout,
        )

    @asynccontextmanager
    async def _create_streamable_http_client(self) -> AsyncIterator[Any]:
        # Every stateless call snapshots this turn's headers. No ambient proxy,
        # persistent credential, or new listener on the Docker host gateway.
        denial = None

        async def note_denial(response: httpx.Response) -> None:
            nonlocal denial
            status = response.status_code
            if status not in (401, 403):
                return
            reason = "authentication required" if status == 401 else "access denied"
            # Only recognize known CM diagnostics. Never relay arbitrary response
            # bodies, which can contain deployment information or credentials.
            length = response.headers.get("content-length", "")
            if (
                length.isdecimal()
                and int(length) <= 512
                and "content-encoding" not in response.headers
            ):
                await response.aread()
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("detail") in (
                    "API token required",
                    "Invalid API token",
                    "Local access requires a safe loopback client",
                ):
                    reason = payload["detail"]
            denial = f"DataBridge HTTP {status}: {reason}"

        async with httpx.AsyncClient(
            headers=self._runtime_headers,
            timeout=self.mcp_config.timeout,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"response": [note_denial]},
        ) as client:
            try:
                async with streamable_http_client(
                    url=self.mcp_config.url, http_client=client
                ) as transport:
                    yield transport
            except ExceptionGroup as exc:
                if denial is None:
                    raise
                # The SDK's TaskGroup otherwise masks the HTTP refusal.
                raise PermissionError(denial) from exc
