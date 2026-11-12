# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.auth_middleware import get_access_token
from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.mcp import (
    McpSchema,
    McpToolSchema,
    McpToolsUpdateRequest,
    McpWriteRequest,
)
from qwenpaw_data.host.core.domain.identity import Identity
from agentscope.mcp import MCPClient

from qwenpaw_data.host.core.domain.mcp import Mcp
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.mcp import public_tool
from qwenpaw_data.host.core.utils.safe_name import require_safe_name

router = APIRouter(prefix="/mcp", tags=["mcp"])

_DEFAULT_AGENT_ID = "default"


def _raise(exc: Exception) -> None:
    mapped = map_domain_error(exc)
    if mapped is not None:
        raise mapped
    raise


def _paths(identity: Identity, agent_id: str = _DEFAULT_AGENT_ID) -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=identity,
        agent_id=require_safe_name(agent_id),
        session_id="default",
    )


def _repo(identity: Identity) -> McpRepository:
    return McpRepository(_paths(identity).mcp_manifest)


def _schema(mcp: Mcp) -> dict[str, Any]:
    return McpSchema.model_validate(mcp.public_record()).model_dump()


def _from_write(body: McpWriteRequest, *, source: str) -> Mcp:
    if (
        body.mcp_config.type == "stdio_mcp"
        and not get_settings().mcp.allow_custom_stdio
    ):
        raise_api(
            "VALIDATION",
            "STDIO MCP is not allowed in this deployment",
            status=400,
        )
    return Mcp(
        client=MCPClient(
            name=body.name,
            is_stateful=body.is_stateful,
            mcp_config=body.mcp_config,
            enable_tools=body.enable_tools,
            disable_tools=body.disable_tools,
            execution_timeout=body.execution_timeout,
        ),
        enabled=body.enabled,
        source=source,
        description=body.description,
    )


def _tool_schema(tool: Any, mcp: Mcp) -> dict[str, Any]:
    return McpToolSchema.model_validate(
        public_tool(
            tool,
            enable_tools=mcp.client.enable_tools,
            disable_tools=mcp.client.disable_tools,
        ),
    ).model_dump()


@router.get("")
async def list_mcps(
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        items = _repo(identity).list()
    except Exception as exc:
        _raise(exc)
    return {"mcps": [_schema(item) for item in items]}


@router.post("")
async def create_mcp(
    body: McpWriteRequest,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp = _repo(identity).create(_from_write(body, source="custom"))
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.get("/{name}/tools")
async def list_mcp_tools(
    name: str,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp, tools = await _repo(identity).list_tools(
            name,
            access_token=get_access_token(),
        )
    except Exception as exc:
        _raise(exc)
    return {"tools": [_tool_schema(tool, mcp) for tool in tools]}


@router.put("/{name}/tools")
async def update_mcp_tools(
    name: str,
    body: McpToolsUpdateRequest,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp = _repo(identity).set_tools(name, body.enable_tools, body.disable_tools)
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.post("/{name}/enable")
async def enable_mcp(
    name: str,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp = _repo(identity).enable(name)
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.post("/{name}/disable")
async def disable_mcp(
    name: str,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp = _repo(identity).disable(name)
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.get("/{name}")
async def get_mcp(
    name: str,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        mcp = _repo(identity).require(name)
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.put("/{name}")
async def update_mcp(
    name: str,
    body: McpWriteRequest,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        if body.name != name:
            raise ValueError("MCP name cannot be changed")
        mcp = _repo(identity).update(_from_write(body, source=body.source))
    except Exception as exc:
        _raise(exc)
    return _schema(mcp)


@router.delete("/{name}")
async def delete_mcp(
    name: str,
    identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    try:
        _repo(identity).delete(name)
    except Exception as exc:
        _raise(exc)
    return {"deleted": name}
