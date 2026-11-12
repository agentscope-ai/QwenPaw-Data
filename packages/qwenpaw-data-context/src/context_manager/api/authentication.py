"""Adapt HTTP authentication to CM Auth-owned AuthorizationContext policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from qwenpaw_data_auth.authorization import AuthorizationContext, authorize_request
from qwenpaw_data_auth.config import AuthConfig
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..auth import (
    AuthorizationError,
    active_target,
    bind_authorization,
    require_target,
)
from ..compatibility.aligned_principal import project_aligned_principal
from ..mcp import MCP_PATH
from ..model import bind_usage_context
from ..runtime import ContextRuntime
from ..store.neo4j import GraphTarget
from ..store.runtime import ExecutionTarget
from .auth import (
    ALL_SCOPES,
    LEGACY_PRINCIPAL,
    SCOPE_QUERY,
    SCOPE_WRITE,
    AuthPrincipal,
    authenticate_api_token,
    extract_bearer_token,
    get_configured_api_keys,
)
from .auth_middleware import bind_principal as bind_aligned_principal
from .authorization import required_scopes_for_request

Authorizer = Callable[[Request], AuthorizationContext]
RequestHandler = Callable[[Request], Awaitable[Response]]
_MCP_DISCOVERY_METHODS = frozenset(
    {"initialize", "notifications/initialized", "ping", "tools/list"}
)
_ROLE_SCOPES = {
    "tenant_admin": ALL_SCOPES,
    "ws_admin": ALL_SCOPES,
    "member": frozenset({SCOPE_QUERY, SCOPE_WRITE}),
}
# A scoped key is the whole credential; these would select another identity.
_KEY_CONFLICTING_HEADERS = (
    "x-qwenpaw-data-api-token",
    "x-qwenpaw-data-service-secret",
    "x-user-id",
)


def rbac_scopes(authorization: AuthorizationContext) -> frozenset[str]:
    """Return the API scopes the active Workspace role may exercise."""
    if authorization.is_platform_admin:
        return frozenset()
    return _ROLE_SCOPES.get(authorization.active_workspace_role, frozenset())


def _scoped_api_key(request: Request) -> AuthPrincipal | None:
    """Return the QWENPAW_DATA_API_KEYS entry presented as the Bearer credential.

    JWTs, invalid values and the full-scope QWENPAW_DATA_API_TOKEN return None and
    stay with the authorizer, which owns identity and rejects them as needed.
    """
    principal = authenticate_api_token(
        extract_bearer_token(request.headers.get("authorization"))
    )
    return None if principal is None or principal == LEGACY_PRINCIPAL else principal


def _presents_credentials(request: Request) -> bool:
    return (
        "authorization" in request.headers
        or "qwenpaw_data_access" in request.cookies
        or "x-qwenpaw-data-api-token" in request.headers
        or "x-qwenpaw-data-service-secret" in request.headers
    )


def _authenticate(
    request: Request, auth_config: AuthConfig, authorizer: Authorizer
) -> tuple[AuthorizationContext, AuthPrincipal | None]:
    """Resolve the caller's identity and any scoped API key that caps it.

    A scoped key stands for the local identity, like the full-scope API token:
    keys never name a multi-user account. The local identity is still resolved
    by the authorizer, so account state and its loopback/host rules apply; the
    caller then caps the grant to the key's scopes.
    """
    api_key = _scoped_api_key(request)
    if api_key is None:
        if (
            auth_config.local_mode
            and get_configured_api_keys()
            and not _presents_credentials(request)
        ):
            # Configured keys are a machine-token requirement, as in local mode
            # with QWENPAW_DATA_API_TOKEN; anonymous loopback must not bypass it.
            raise HTTPException(
                401, "API token required", headers={"WWW-Authenticate": "Bearer"}
            )
        return authorizer(request), None
    if not auth_config.local_mode:
        raise HTTPException(403, "Scoped API keys are disabled in multi-user mode")
    if (
        len(request.headers.getlist("authorization")) > 1
        or "qwenpaw_data_access" in request.cookies
        or any(name in request.headers for name in _KEY_CONFLICTING_HEADERS)
    ):
        raise HTTPException(401, "Conflicting credentials")
    headers = [
        (name, value)
        for name, value in request.scope["headers"]
        if name.lower() != b"authorization"
    ]
    if auth_config.api_token:
        # Local identity requires the configured API token; a valid key stands in.
        headers.append(
            (b"authorization", b"Bearer " + auth_config.api_token.encode("utf-8"))
        )
    local = Request({**request.scope, "headers": headers}, request.receive)
    return authorizer(local), api_key


def _request_principal(
    request: Request,
    authorization: AuthorizationContext,
    api_key: AuthPrincipal | None,
) -> AuthPrincipal:
    """Intersect the RBAC grant with every API key that authenticated the request."""
    scopes = rbac_scopes(authorization)
    upstream = getattr(request.state, "auth_principal", None)
    if type(upstream) is AuthPrincipal:
        scopes &= upstream.scopes
    if api_key is not None:
        return AuthPrincipal(
            api_key.subject, scopes & api_key.scopes, api_key.credential_id
        )
    payload = authorization.payload if isinstance(authorization.payload, dict) else {}
    return AuthPrincipal(
        authorization.user_id,
        scopes,
        "local" if payload.get("local") is True else "session",
    )


def _route_scope_denial(request: Request, principal: AuthPrincipal) -> Response | None:
    """Apply the fail-closed route policy to callers capped by a scoped key."""
    required = required_scopes_for_request(request.method, request.url.path)
    if required is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "No authorization policy for this endpoint"},
        )
    if principal.has_scopes(required):
        return None
    return JSONResponse(
        status_code=403,
        content={"detail": "Insufficient scope", "required_scopes": sorted(required)},
        headers={
            "WWW-Authenticate": 'Bearer error="insufficient_scope", '
            f'scope="{" ".join(sorted(required))}"'
        },
    )


async def _is_mcp_discovery_request(request: Request) -> bool:
    """Allow anonymous setup and static discovery at the MCP endpoint."""
    if request.method != "POST":
        return False
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and isinstance(body.get("method"), str)
        and body["method"] in _MCP_DISCOVERY_METHODS
    )


def install_context_authentication(
    application: FastAPI,
    auth_config: AuthConfig,
    target: ExecutionTarget | None = None,
    *,
    authorizer: Authorizer = authorize_request,
) -> None:
    """Authenticate managed requests and bind their trusted graph target."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI app")
    if type(auth_config) is not AuthConfig:
        raise TypeError("auth_config must be an AuthConfig")
    if target is not None and not isinstance(target, ExecutionTarget):
        raise TypeError("target must be an ExecutionTarget or None")
    runtime = getattr(application.state, "context_runtime", None)
    if type(runtime) is not ContextRuntime or runtime.closed:
        raise RuntimeError("ContextRuntime must be bound before authentication")
    if runtime.fixed_target != target:
        raise RuntimeError("authentication target must match ContextRuntime")
    if hasattr(application.state, "auth_config"):
        raise RuntimeError("context authentication is already installed")
    # Reject a malformed key registry at startup instead of on each request.
    get_configured_api_keys()
    application.state.auth_config = auth_config
    if target is not None:
        application.state.context_target = target

    @application.middleware("http")
    async def authenticate_context_request(
        request: Request,
        call_next: RequestHandler,
    ) -> Response:
        """Authenticate managed APIs and restore request-local auth contexts."""
        mcp_request = request.url.path.rstrip("/") == MCP_PATH
        managed = (
            mcp_request
            or request.url.path
            in (
                "/api/v1/config",
                "/api/v1/docs",
                "/api/v1/cm/domains",
                "/api/v1/cm/search-metrics",
                "/api/v1/cm/dimensions",
                "/api/v1/cm/datasets",
                "/api/v1/admin/cypher",
                "/api/v1/admin/cypher/raw",
                "/api/v1/semantic/feedback_card",
                # TODO(cm-post-merge): remove with the temporary selection routes.
                "/api/datasources/active",
                "/api/v1/cm/datasources/active",
            )
            or request.url.path.startswith(
                (
                    "/api/cm/",
                    "/api/v1/graph/",
                    "/api/v1/rdb/",
                    "/api/v1/config/",
                    "/api/v1/embed/",
                    # TODO(cm-api-compat): remove this managed prefix together with
                    # the legacy datasource-types forwarder after frontend migration.
                    "/api/datasource-types",
                    "/api/semantic-config/",
                    "/api/system/model-config",
                    "/api/v1/admin/explorer/",
                    "/api/v3/cm/explore",
                    "/api/v1/docs/",
                    "/api/v1/cm/downloads/",
                    # TODO(cm-api-compat): remove this managed prefix together
                    # with the legacy KG forwarding router.
                    "/api/v1/admin/kg/",
                    "/api/v1/ug/",
                    "/api/v1/mg/userspace/",
                )
            )
        )
        if request.method == "OPTIONS" or not managed:
            return await call_next(request)
        if mcp_request and await _is_mcp_discovery_request(request):
            return await call_next(request)
        try:
            authorization, api_key = _authenticate(request, auth_config, authorizer)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        try:
            tenant_id, workspace_id = active_target(authorization)
            selected_target = target or GraphTarget(tenant_id, workspace_id)
            require_target(
                authorization,
                tenant_id=selected_target.tenant_id,
                workspace_id=selected_target.workspace_id,
            )
            aligned_principal = project_aligned_principal(authorization)
        except AuthorizationError as exc:
            return JSONResponse(
                status_code=403,
                content={"detail": str(exc)},
            )
        principal = _request_principal(request, authorization, api_key)
        if api_key is not None:
            denial = _route_scope_denial(request, principal)
            if denial is not None:
                return denial
        request.state.cm_authorization = authorization
        # MCP tools and other scope checks read the capped grant from here.
        request.state.auth_principal = principal
        parts = request.url.path.strip("/").replace("-", "_").split("/")
        while parts and parts[0] in {"api", "v1", "v2", "v3", "cm"}:
            parts.pop(0)
        source_step = "_".join(parts[-2:]) or "cm_console"
        with (
            bind_authorization(authorization),
            bind_aligned_principal(aligned_principal),
            bind_usage_context(
                session_id=request.headers.get("x-cm-host-session-id", "").strip(),
                chat_id=request.headers.get("x-cm-host-chat-id", "").strip(),
                source="上下文工作台",
                source_step=source_step,
                probe=False,
            ),
        ):
            return await call_next(request)


__all__ = ["Authorizer", "install_context_authentication", "rbac_scopes"]
