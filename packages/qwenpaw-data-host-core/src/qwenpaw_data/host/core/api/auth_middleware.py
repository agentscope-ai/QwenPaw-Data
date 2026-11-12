# -*- coding: utf-8 -*-
"""Host auth gate: every request is authenticated by ``qwenpaw_data_auth``.

``request_identity_payload`` selects the credential and ``principal_from_payload``
revalidates user, tenant, workspace and role against the control-plane DB on
every request. Invalid, forged or ambiguous credentials are rejected on every
path; nothing falls back to the local administrator.

* Local mode: a safe loopback peer without credentials is the reserved local
  admin, unless ``QWENPAW_DATA_API_TOKEN`` is configured (then it is required).
  The static token is a trusted machine credential accepted from any peer.
* Multi-user mode: ``qwenpaw_data_access`` cookie or Bearer JWT only.
* ``X-User-Id`` delegation requires a valid service secret.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import ContextVar

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from qwenpaw_data_auth import AuthConfig, Principal, bootstrap_local
from qwenpaw_data_auth.authentication import (
    AuthenticationError,
    extract_access_token,
    principal_from_payload,
)
from qwenpaw_data_auth.config import runtime_mode
from qwenpaw_data_auth.local import request_identity_payload

from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "qwenpaw_data_access"

ANONYMOUS_PRINCIPAL = Principal(
    user_id="",
    tenant_id="",
    workspace_ids=[],
    active_workspace_id="",
    role="anonymous",
)

# Unbound context is anonymous: code outside the gate never sees an admin.
principal_ctx: ContextVar[Principal] = ContextVar(
    "host_principal_ctx", default=ANONYMOUS_PRINCIPAL
)
identity_ctx: ContextVar[Identity | None] = ContextVar(
    "host_identity_ctx", default=None
)
# The caller's verified user JWT only; never the API token or service secret.
access_token_ctx: ContextVar[str | None] = ContextVar(
    "host_access_token_ctx", default=None
)

# Accessible with in the whole process.
# so non-HTTP paths (e.g. IM channels)
# can get access token to call APIs.
_identity_access_tokens: dict[Identity, str] = {}
_identity_tokens_lock = threading.Lock()
_auth_config_lock = threading.Lock()

# No principal is resolved for these.
_EXEMPT_PATHS = frozenset({"/health", "/api/v1/health", "/favicon.ico"})
# Real principal when credentials are valid, anonymous otherwise.
_PUBLIC_PATHS = frozenset({"/api/auth/status", "/api/v1/auth/status"})


def get_principal() -> Principal:
    return principal_ctx.get()


def is_authenticated() -> bool:
    return principal_ctx.get() is not ANONYMOUS_PRINCIPAL


def get_identity() -> Identity:
    identity = identity_ctx.get()
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Not authenticated"},
        )
    return identity


def get_access_token() -> str | None:
    return access_token_ctx.get()


def get_identity_access_token(identity: Identity) -> str | None:
    with _identity_tokens_lock:
        return _identity_access_tokens.get(identity)


def _record_identity_token(identity: Identity, token: str) -> None:
    with _identity_tokens_lock:
        _identity_access_tokens[identity] = token


def auth_enabled() -> bool:
    """Multi-user login is required; local mode never needs a login."""
    return runtime_mode() == "multi-user"


def is_exempt_path(path: str) -> bool:
    return path in _EXEMPT_PATHS


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS


def validate_startup_configuration() -> None:
    cfg = AuthConfig.from_env()
    if cfg.env.strip().lower() in {"prod", "production"} and not cfg.secret_keys:
        raise RuntimeError(
            "QWENPAW_DATA_AUTH_SECRET is required when QWENPAW_DATA_ENV is production"
        )
    if cfg.local_mode:
        # Signing and service secrets persist under QWENPAW_DATA_HOME/auth.
        return
    # Multi-user execution mints delegated tokens through the Auth service
    # (utils.auth.generate_temporary_token).
    if not cfg.auth_url:
        raise RuntimeError("QWENPAW_DATA_AUTH_URL is required in multi-user mode")
    if not cfg.service_secret:
        raise RuntimeError(
            "QWENPAW_DATA_SERVICE_SECRET is required in multi-user mode"
        )


def _auth_config(app) -> AuthConfig:
    """Resolved once by the lifespan; bootstrapped lazily when it did not run."""
    cfg = getattr(app.state, "auth_config", None)
    if cfg is not None:
        return cfg
    with _auth_config_lock:
        cfg = getattr(app.state, "auth_config", None)
        if cfg is None:
            cfg = bootstrap_local(AuthConfig.from_env())
            app.state.auth_config = cfg
    return cfg


def _authenticate(request: Request) -> tuple[Principal, Identity, str | None]:
    """Blocking (control-plane DB): run off the event loop."""
    cfg = _auth_config(request.app)
    # allow_machine: the static API token is a machine credential from any
    # peer; X-User-Id is honoured only with a valid service secret.
    payload = request_identity_payload(request, cfg, allow_machine=True)
    principal = principal_from_payload(payload, cfg.control_plane_dsn)[0]
    try:
        identity = Identity.from_principal(principal)
    except ValueError as exc:
        raise AuthenticationError("Invalid principal", 403) from exc
    token = None
    if not payload.get("local") and not payload.get("machine"):
        # A verified user JWT (cookie or Bearer) is the only forwarded token.
        token = extract_access_token(
            request.cookies.get(ACCESS_COOKIE),
            request.headers.get("authorization") or "",
        )
    return principal, identity, token or None


async def _with_auth(
    request,
    call_next,
    *,
    principal: Principal,
    identity: Identity | None,
    token: str | None,
):
    p_tok = principal_ctx.set(principal)
    i_tok = identity_ctx.set(identity)
    t_tok = access_token_ctx.set(token)
    if token is not None and identity is not None:
        _record_identity_token(identity, token)
    try:
        return await call_next(request)
    finally:
        access_token_ctx.reset(t_tok)
        identity_ctx.reset(i_tok)
        principal_ctx.reset(p_tok)


async def auth_middleware(request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or is_exempt_path(path):
        return await call_next(request)
    try:
        principal, identity, token = await asyncio.to_thread(_authenticate, request)
    except AuthenticationError as exc:
        if is_public_path(path):
            return await _with_auth(
                request,
                call_next,
                principal=ANONYMOUS_PRINCIPAL,
                identity=None,
                token=None,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception:
        logger.exception("Host authentication is unavailable")
        return JSONResponse(
            status_code=503, content={"detail": "Authentication unavailable"}
        )
    return await _with_auth(
        request,
        call_next,
        principal=principal,
        identity=identity,
        token=token,
    )
