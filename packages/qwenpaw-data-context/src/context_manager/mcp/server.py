"""Mount one request-authorized MCP server for native and App-compatible tools."""

from functools import partial

from fastapi import FastAPI, Request
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from qwenpaw_data_auth.authorization import AuthorizationContext

from ..runtime import ContextRuntime
from .graph import ResolveContext, register_graph_tools
from .harness_tool import patch_mcp_tool_manager
from .http_common import _transport_security
from .legacy import LEGACY_TOOL_NAMES, register_legacy_tools
from .sql import register_sql_tools

MCP_PATH = "/mcp/v1/cm"
NATIVE_TOOL_NAMES = frozenset({
    "search_graph_fulltext", "search_graph_vector", "get_graph_schema", "query_graph",
    "execute_read", "execute_create", "execute_modify",
})
_WRITE_TOOLS = frozenset({"execute_create", "execute_modify"})
# Public scope names; they equal context_manager.api.auth.SCOPE_WRITE/SCOPE_QUERY,
# which this module must not import at load time (api imports mcp).
TOOL_SCOPE_POLICIES = {
    name: frozenset({"write" if name in _WRITE_TOOLS else "query"})
    for name in LEGACY_TOOL_NAMES | NATIVE_TOOL_NAMES
}


def resolve_mcp_context(
    application: FastAPI, ctx: Context, tool_name: str,
) -> tuple[ContextRuntime, AuthorizationContext]:
    from ..api.auth import AuthPrincipal

    request = ctx.request_context.request
    if not isinstance(request, Request):
        raise ToolError("An authenticated HTTP request is required")
    principal = getattr(request.state, "auth_principal", None)
    required = TOOL_SCOPE_POLICIES.get(tool_name)
    if required is None or type(principal) is not AuthPrincipal or not principal.has_scopes(required):
        raise ToolError("Insufficient tool scope")
    authorization = getattr(request.state, "cm_authorization", None)
    if type(authorization) is not AuthorizationContext:
        raise ToolError("An authenticated HTTP request is required")
    if tool_name in _WRITE_TOOLS and authorization.active_workspace_role not in {"ws_admin", "tenant_admin"}:
        raise ToolError("SQL writes require workspace administration")
    runtime = getattr(application.state, "context_runtime", None)
    if type(runtime) is not ContextRuntime or runtime.closed:
        raise ToolError("Context runtime is unavailable")
    return runtime, authorization


def create_mcp_server(application: FastAPI, *, resolve_context: ResolveContext | None = None) -> FastMCP:
    mcp = FastMCP(
        "context_manager", host="127.0.0.1", streamable_http_path=MCP_PATH,
        stateless_http=True, json_response=True, transport_security=_transport_security(),
    )
    patch_mcp_tool_manager(mcp)
    resolver = resolve_context or partial(resolve_mcp_context, application)
    register_graph_tools(mcp, resolver)
    register_sql_tools(mcp, resolver)
    register_legacy_tools(mcp, resolver, application)
    return mcp


def mount_mcp(application: FastAPI) -> FastMCP:
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI app")
    if hasattr(application.state, "context_mcp"):
        raise RuntimeError("Context MCP is already mounted")
    mcp = create_mcp_server(application)
    application.router.routes.extend(mcp.streamable_http_app().routes)
    application.state.context_mcp = mcp
    return mcp


__all__ = ["MCP_PATH", "NATIVE_TOOL_NAMES", "TOOL_SCOPE_POLICIES", "create_mcp_server", "mount_mcp"]
