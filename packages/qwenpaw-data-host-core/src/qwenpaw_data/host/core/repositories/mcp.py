# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from agentscope.mcp import MCPClient
from agentscope.workspace._gateway_client import GatewayMCPClient

from qwenpaw_data.host.core.domain.mcp import (
    Mcp,
    McpConflictError,
    McpConnectFailed,
    McpNotFoundError,
)
from qwenpaw_data.host.core.integrations.cm.mcp import (
    apply_cm_settings_url,
    is_cm_mcp_client,
    project_workspace_mcp_client,
    set_cm_mcp_headers,
)
from qwenpaw_data.host.core.query_log import Step, log_error, log_info
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.mcp import (
    load_builtin_mcp_records,
    read_mcp_manifest,
    write_mcp_manifest,
)
from qwenpaw_data.host.core.utils.mcp_headers import merge_mcp_headers
from qwenpaw_data.host.core.utils.safe_name import require_safe_name

logger = logging.getLogger(__name__)

_SPEC_KEYS = (
    "name",
    "is_stateful",
    "mcp_config",
    "enable_tools",
    "disable_tools",
    "execution_timeout",
)


class McpRepository:
    """Agent MCP inventory: ``{agent}/workspace/mcp.json``."""

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = Path(manifest_path)

    def list(self) -> list[Mcp]:
        return list(self._items().values())

    def require(self, name: str) -> Mcp:
        safe = require_safe_name(name)
        items = self._items()
        if safe not in items:
            raise McpNotFoundError(f"MCP not found: {safe}")
        return items[safe]

    def create(self, mcp: Mcp) -> Mcp:
        items = self._items()
        if mcp.name in items:
            raise McpConflictError(f"MCP already exists: {mcp.name}")
        return self._put(items, mcp)

    def update(self, mcp: Mcp) -> Mcp:
        items = self._items()
        if mcp.name not in items:
            raise McpNotFoundError(f"MCP not found: {mcp.name}")
        return self._put(items, mcp)

    def delete(self, name: str) -> None:
        items = self._items()
        items.pop(self.require(name).name)
        self._write(items)

    def enable(self, name: str) -> Mcp:
        return self.update(replace(self.require(name), enabled=True))

    def disable(self, name: str) -> Mcp:
        return self.update(replace(self.require(name), enabled=False))

    def set_tools(
        self,
        name: str,
        enable_tools: list[str] | None,
        disable_tools: list[str] | None,
    ) -> Mcp:
        current = self.require(name)
        return self.update(
            replace(
                current,
                client=current.client.model_copy(
                    update={
                        "enable_tools": enable_tools,
                        "disable_tools": disable_tools,
                    },
                ),
            ),
        )

    async def sync(
        self,
        workspace: Any,
        *,
        agent_id: str,
        session_id: str,
        request_id: str,
        mcp_headers: dict[str, dict[str, str]],
        stream: Any | None = None,
        access_token: str | None = None,
        datasource_id: str | None = None,
        dataset_names: list[str] | None = None,
        context_model_header: str | None = None,
    ) -> None:
        scope = {"agent_id": agent_id, "session_id": session_id}
        wanted = {
            mcp.name: project_workspace_mcp_client(mcp.to_client())
            for mcp in self.list()
            if mcp.enabled
        }
        # Configured headers reach runs that never pass a gateway entry (cron,
        # IM, run_now) and would otherwise register without them; the request
        # overlay wins wherever both name the same header.
        configured = get_settings().mcp.registration_headers
        for name, values in merge_mcp_headers(configured, mcp_headers).items():
            if name not in wanted:
                continue
            client = wanted[name]
            headers = httpx.Headers(client.mcp_config.headers)
            headers.update(values)
            client.mcp_config.headers = dict(headers)

        live = {client.name: client for client in await workspace.list_mcps(**scope)}
        for name in set(live) - set(wanted):
            await _remove_workspace_mcp(workspace, live[name], **scope)
        for name, spec in wanted.items():
            reuse = name in live and _same_client(live[name], spec)
            client = live[name] if reuse else spec
            try:
                if not reuse:
                    if name in live:
                        await _remove_workspace_mcp(workspace, live[name], **scope)
                    await workspace.add_mcp(client, **scope)
                    registered = await workspace.list_mcps(**scope)
                    client = next(item for item in registered if item.name == name)
                if is_cm_mcp_client(client):
                    await set_cm_mcp_headers(
                        client,
                        access_token,
                        datasource_id=datasource_id,
                        dataset_names=dataset_names,
                        session_id=session_id,
                        chat_id=request_id,
                        context_model_header=context_model_header,
                    )
                tools = await asyncio.wait_for(
                    client.list_raw_tools(),
                    timeout=get_settings().mcp.probe_timeout,
                )
            except Exception as exc:
                event = await _log_mcp(
                    request_id, client, session_id, stream=stream, error=exc
                )
                if is_cm_mcp_client(client):
                    raise McpConnectFailed(event["error"]) from exc
            else:
                await _log_mcp(
                    request_id,
                    client,
                    session_id,
                    stream=stream,
                    tool_count=len(tools),
                )

    async def list_tools(
        self,
        name: str,
        *,
        access_token: str | None = None,
    ) -> tuple[Mcp, list[Any]]:
        mcp = self.require(name)
        client = mcp.to_client()
        if (
            client.mcp_config.type == "stdio_mcp"
            and not get_settings().mcp.allow_custom_stdio
        ):
            # Connecting a stdio client spawns its command in this process.
            raise ValueError(f"STDIO MCP must not connect here: {mcp.name}")
        if is_cm_mcp_client(client):
            await set_cm_mcp_headers(client, access_token)
        try:
            if client.is_stateful:
                await client.connect()
            await client.list_raw_tools()
            return mcp, list(getattr(client, "_cached_tools", None) or [])
        finally:
            if client.is_stateful and client.is_connected:
                await client.close()

    def _items(self) -> dict[str, Mcp]:
        self._ensure_builtins()
        items = {
            name: Mcp.from_record(apply_cm_settings_url({**record, "name": name}))
            for name, record in read_mcp_manifest(self.manifest_path).items()
        }
        # The manifest lives in the user-writable workspace, so its records
        # are untrusted input regardless of the claimed source: a stdio
        # entry means "spawn this command", and the host process must never
        # honor that from untrusted config.
        if not get_settings().mcp.allow_custom_stdio:
            for name, mcp in list(items.items()):
                if mcp.client.mcp_config.type == "stdio_mcp":
                    logger.warning(
                        "ignoring stdio MCP %s from untrusted manifest %s",
                        name,
                        self.manifest_path,
                    )
                    items.pop(name)
        return dict(sorted(items.items()))

    def _ensure_builtins(self) -> None:
        records = read_mcp_manifest(self.manifest_path)
        added = False
        for name, record in load_builtin_mcp_records().items():
            if name in records:
                continue
            records[name] = dict(record)
            added = True
        if added:
            write_mcp_manifest(self.manifest_path, records)

    def _put(self, items: dict[str, Mcp], mcp: Mcp) -> Mcp:
        mcp.to_client()
        if (
            mcp.client.mcp_config.type == "stdio_mcp"
            and not get_settings().mcp.allow_custom_stdio
        ):
            raise ValueError(f"STDIO MCP is not allowed: {mcp.name}")
        items[mcp.name] = mcp
        self._write(items)
        return mcp

    def _write(self, items: dict[str, Mcp]) -> None:
        records: dict[str, dict[str, Any]] = {}
        for mcp in items.values():
            record = mcp.to_record()
            name = record.pop("name")
            records[name] = record
        write_mcp_manifest(self.manifest_path, records)


async def _remove_workspace_mcp(
    workspace: Any,
    client: MCPClient,
    *,
    agent_id: str,
    session_id: str,
) -> None:
    if isinstance(client, GatewayMCPClient):
        # Even stateless proxies own a gateway registration. Keep local state
        # until deregistration succeeds; the SDK skips closing stateless MCPs.
        await client.close(ignore_errors=False)
    await workspace.remove_mcp(
        client.name,
        agent_id=agent_id,
        session_id=session_id,
    )


async def _log_mcp(
    request_id: str,
    client: MCPClient,
    session_id: str,
    *,
    stream: Any | None = None,
    error: Exception | None = None,
    **extra: Any,
) -> dict[str, Any]:
    cfg = client.mcp_config
    event = {"mcp_name": client.name, "status": "failed" if error else "connected"}
    if error is not None:
        event["error"] = str(error) or type(error).__name__
    if cfg.type == "http_mcp":
        event["url"] = cfg.url
    else:
        event["command"] = cfg.command
    if error is None:
        log_info(
            request_id=request_id,
            step=Step.MCP_CONNECT,
            message="MCP connected",
            context={**event, "session_id": session_id, **extra},
        )
    else:
        log_error(
            request_id=request_id,
            step=Step.MCP_CONNECT_FAILED,
            message="MCP connect failed",
            context={**event, "session_id": session_id, **extra},
        )
    if stream is not None:
        await stream.mcp_connect(**event)
    return event


def _same_client(left: MCPClient, right: MCPClient) -> bool:
    return _fingerprint(left) == _fingerprint(right)


def _fingerprint(client: MCPClient) -> dict[str, Any]:
    data = client.model_dump(mode="json")
    config = data.get("mcp_config")
    if isinstance(config, dict) and isinstance(config.get("headers"), dict):
        data = {
            **data,
            "mcp_config": {
                **config,
                "headers": {
                    key: value
                    for key, value in config["headers"].items()
                    if str(key).lower() != "authorization"
                },
            },
        }
    return {key: data.get(key) for key in _SPEC_KEYS}
