# -*- coding: utf-8 -*-
"""Context Manager MCP helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agentscope.mcp import MCPClient

from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.settings.cm import CmSettings


def is_cm_mcp_name(name: str) -> bool:
    return name == CmSettings.MCP_NAME


def is_cm_mcp_client(client: MCPClient) -> bool:
    return is_cm_mcp_name(client.name)


def apply_cm_settings_url(record: dict[str, Any]) -> dict[str, Any]:
    """Fill builtin CM url from settings. Inventory url is ignored."""
    name = str(record.get("name") or "")
    if not is_cm_mcp_name(name):
        return record
    config = record.get("mcp_config")
    if not isinstance(config, dict):
        raise ValueError(f"invalid CM MCP mcp_config: {name}")
    record["mcp_config"] = {**config, "url": get_settings().cm.mcp_url()}
    return record


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def rewrite_loopback_url_for_docker(url: str, alias: str | None = None) -> str:
    """Point a Host-loopback URL at the Docker host gateway alias.

    Only the hostname is replaced (port, userinfo, path and query are kept);
    ``localhost.example.com`` or ``127.0.0.10`` are not loopback and stay.
    """
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return url
    if hostname is None or hostname.lower() not in _LOOPBACK_HOSTS:
        return url
    target = (alias or get_settings().workspace.docker_loopback_alias()).strip()
    netloc = target if port is None else f"{target}:{port}"
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def project_workspace_mcp_client(client: MCPClient) -> MCPClient:
    """Select the workspace's CM address without changing the Host inventory."""
    if not is_cm_mcp_client(client):
        return client
    settings = get_settings()
    if settings.workspace.is_cloud_sandbox:
        url = settings.cm.sandbox_mcp_url()
    elif settings.workspace.backend == "docker":
        url = rewrite_loopback_url_for_docker(client.mcp_config.url)
    else:
        return client
    if url == client.mcp_config.url:
        return client
    return client.model_copy(
        update={"mcp_config": client.mcp_config.model_copy(update={"url": url})},
    )


async def set_cm_mcp_headers(
    client: MCPClient,
    access_token: str | None,
    *,
    datasource_id: str | None = None,
    dataset_names: list[str] | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
) -> None:
    headers: dict[str, str] = {}
    if token := (access_token or "").strip():
        headers["Authorization"] = f"Bearer {token}"
    if ds := (datasource_id or "").strip():
        headers["datasource_id"] = ds
    if dataset_names:
        headers["dataset_name"] = ",".join(dataset_names)
    if sid := (session_id or "").strip():
        headers["session_id"] = sid
    if cid := (chat_id or "").strip():
        headers["chat_id"] = cid
    await client.set_runtime_headers(headers)


async def ensure_workspace_cm_mcp(
    workspace: Any,
    *,
    agent_id: str,
    session_id: str,
    chat_id: str,
    access_token: str | None = None,
    datasource_id: str | None = None,
    dataset_names: list[str] | None = None,
) -> None:
    for client in await workspace.list_mcps(agent_id=agent_id, session_id=session_id):
        if is_cm_mcp_client(client):
            await set_cm_mcp_headers(
                client,
                access_token,
                datasource_id=datasource_id,
                dataset_names=dataset_names,
                session_id=session_id,
                chat_id=chat_id,
            )
