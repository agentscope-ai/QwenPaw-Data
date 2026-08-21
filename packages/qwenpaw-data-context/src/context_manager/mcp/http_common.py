"""Shared Streamable HTTP MCP settings for the unified CM server."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)


def is_loopback_bind_host(host: str) -> bool:
    """Whether a bind target is strictly local (hostname or IP literal)."""
    value = host.strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class _ApiTokenVerifier(TokenVerifier):
    """MCP SDK verifier for the same token used by the REST API."""

    async def verify_token(self, token: str) -> AccessToken | None:
        from context_manager.api.auth import authenticate_api_token

        principal = authenticate_api_token(token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.subject,
            scopes=sorted(principal.scopes),
        )


def _mcp_auth_settings() -> tuple[TokenVerifier | None, AuthSettings | None]:
    """Enable native MCP Bearer auth whenever the API token is configured."""
    from context_manager.api.auth import is_authentication_configured

    if not is_authentication_configured():
        return None, None

    resource_url = (
        os.environ.get("QWENPAW_DATA_MCP_PUBLIC_URL")
        or "http://127.0.0.1:8765/mcp/v1/cm"
    ).strip()
    parsed = urlsplit(resource_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("QWENPAW_DATA_MCP_PUBLIC_URL must be an absolute HTTP(S) URL")
    issuer_url = f"{parsed.scheme}://{parsed.netloc}"
    return _ApiTokenVerifier(), AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        required_scopes=["query"],
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in (os.environ.get(name) or "").split(",") if item.strip()]


def _transport_security() -> TransportSecuritySettings:
    """Apply explicit Host and exact Origin allowlists to HTTP MCP."""
    from context_manager.api.security import configured_cors_origins

    allowed_hosts = [
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        "host.docker.internal",
        "host.docker.internal:*",
        *_csv_env("QWENPAW_DATA_MCP_ALLOWED_HOSTS"),
    ]
    extra_origins_raw = os.environ.get("QWENPAW_DATA_MCP_ALLOWED_ORIGINS") or ""
    extra_origins = (
        configured_cors_origins(extra_origins_raw) if extra_origins_raw.strip() else []
    )
    allowed_origins = [*configured_cors_origins(), *extra_origins]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


def create_streamable_mcp(name: str) -> FastMCP:
    """Create a stateless Streamable HTTP MCP server.

    With ``stateless_http=True``, each POST is handled independently:
    ``tools/list`` and ``tools/call`` do not require ``initialize`` or
    ``Mcp-Session-Id``.     Session state is only relevant inside tool arguments
    (e.g. ``session_ref`` / ``metadata.session_ref`` on CM business APIs).

    ``json_response=True`` (default) returns plain JSON bodies instead of the
    SSE response path, which removes the per-request SSE stream setup/teardown
    as a failure source. Set ``CM_MCP_JSON_RESPONSE=0`` to restore SSE
    responses (A/B switch for diagnosing dropped responses).

    The bind host defaults to loopback; set ``QWENPAW_DATA_MCP_HOST=0.0.0.0`` to
    expose the server externally (make sure access control is in place).
    """
    token_verifier, auth_settings = _mcp_auth_settings()
    return FastMCP(
        name,
        streamable_http_path="/",
        stateless_http=True,
        json_response=_env_flag("CM_MCP_JSON_RESPONSE", True),
        host=(os.environ.get("QWENPAW_DATA_MCP_HOST") or "127.0.0.1").strip(),
        token_verifier=token_verifier,
        auth=auth_settings,
        transport_security=_transport_security(),
    )


def _rpc_item_summary(item: Any) -> str:
    if not isinstance(item, dict):
        return "method=?"
    method = item.get("method") or "?"
    rpc_id = item.get("id")
    tool = ""
    params = item.get("params")
    if method == "tools/call" and isinstance(params, dict):
        tool = f" tool={params.get('name', '?')}"
    id_part = "notification" if rpc_id is None else f"rpc_id={rpc_id}"
    return f"method={method}{tool} {id_part}"


def _rpc_summary(body: bytes) -> str:
    if not body:
        return "method=? (empty body)"
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return "method=? (unparsed body)"
    if isinstance(payload, list):
        return "batch[" + "; ".join(_rpc_item_summary(i) for i in payload) + "]"
    return _rpc_item_summary(payload)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _McpMount:
    """ASGI entry that forwards ``/mcp/v1/cm`` and ``/mcp/v1/cm/`` without 307.

    Starlette ``Mount`` redirects bare paths to a trailing slash. MCP SDK clients
    (httpx with ``follow_redirects=False``) then hang on ``initialize``.

    Also emits protocol-level access logs so responsibility can be pinned:
    ``CM MCP_START`` is written the moment the HTTP request (JSON-RPC body)
    arrives, ``CM MCP_END`` when the HTTP response finishes. A hung call with
    ``MCP_START`` but no ``MCP_END`` is a CM-side failure; a hung client with
    no ``MCP_START`` at all never reached CM.
    """

    def __init__(self, inner: ASGIApp, mount_path: str) -> None:
        self.inner = inner
        self.mount_path = mount_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.inner(scope, receive, send)
            return
        req_path = scope.get("path", "")
        if req_path != self.mount_path and not req_path.startswith(self.mount_path + "/"):
            resp = Response("Not Found", status_code=404)
            await resp(scope, receive, send)
            return
        scope = dict(scope)
        rest = req_path[len(self.mount_path) :] or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        scope["path"] = rest
        scope["root_path"] = scope.get("root_path", "") + self.mount_path

        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        await self._call_http_logged(scope, receive, send)

    async def _call_http_logged(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        from context_manager.mcp.harness_tool import ensure_mcp_access_log_handler

        access_log = ensure_mcp_access_log_handler()
        http_method = str(scope.get("method", "") or "")
        req_id = uuid4().hex[:8]
        run_id = ""
        for key, value in scope.get("headers") or []:
            if key.lower() in (b"x-qwenpaw-data-run", b"x-request-id"):
                run_id = value.decode("latin-1", errors="replace")
                break
        run_part = f" run={run_id}" if run_id else ""

        # Pre-read the JSON-RPC body (small) so MCP_START carries method/id.
        body = b""
        disconnected_early = False
        if http_method == "POST":
            chunks: list[bytes] = []
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected_early = True
                    break
                chunks.append(message.get("body", b"") or b"")
                if not message.get("more_body"):
                    break
            body = b"".join(chunks)
            summary = _rpc_summary(body)
        else:
            summary = f"http={http_method}"

        access_log.info("[%s] CM MCP_START req=%s%s %s", _now(), req_id, run_part, summary)
        if disconnected_early:
            access_log.info(
                "[%s] CM MCP_ABORT req=%s%s %s client disconnected before body",
                _now(), req_id, run_part, summary,
            )
            return

        replayed = False

        async def replay_receive() -> Any:
            nonlocal replayed
            if http_method == "POST" and not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status_code: dict[str, Any] = {"value": None}
        response_finished = False
        t0 = time.monotonic()

        async def wrapped_send(message: Any) -> None:
            nonlocal response_finished
            if message["type"] == "http.response.start":
                status_code["value"] = message.get("status")
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                response_finished = True
            await send(message)

        try:
            await self.inner(scope, replay_receive, wrapped_send)
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            access_log.info(
                "[%s] CM MCP_ABORT req=%s%s %s %dms status=%s exc=%s: %s",
                _now(), req_id, run_part, summary, elapsed_ms,
                status_code["value"], type(exc).__name__, exc,
            )
            raise
        else:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            suffix = "" if response_finished else " (incomplete: response never finished)"
            access_log.info(
                "[%s] CM MCP_END req=%s%s %s status=%s %dms%s",
                _now(), req_id, run_part, summary, status_code["value"], elapsed_ms, suffix,
            )


_MCP_HTTP_METHODS = ["GET", "POST", "DELETE", "OPTIONS", "HEAD"]


def mount_streamable_http(app: Any, mcp: FastMCP, *, path: str) -> Any:
    """Mount MCP sub-app; return session manager for parent ``lifespan``.

    Starlette does not run mounted sub-app lifespans; the parent app must
    ``async with session_manager.run():`` (see ``pkg/main.py`` and
    ``api/server.py``).
    """
    base = path.rstrip("/")
    sub = mcp.streamable_http_app()
    entry = _McpMount(sub, base)
    # Register before Mount-style redirects; both slash forms hit the same handler.
    for route_path in (base, base + "/"):
        app.router.routes.insert(
            0,
            Route(route_path, endpoint=entry, methods=_MCP_HTTP_METHODS),
        )
    return mcp.session_manager
