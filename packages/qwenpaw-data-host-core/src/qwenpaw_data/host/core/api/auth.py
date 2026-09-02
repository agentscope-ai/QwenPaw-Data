# -*- coding: utf-8 -*-
"""Minimal bearer-token auth for the host-core service.

Mirrors the subset of ``context_manager/api/auth.py`` needed here (single
token via ``QWENPAW_DATA_API_TOKEN``, constant-time compare, exempt health
path). Scoped API keys, rate limiting and audit logging follow in a later
wave; extract a shared package before duplicating more of that logic.

When no token is configured the service fails closed for non-loopback
clients instead of running open.
"""

from __future__ import annotations

import ipaddress
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

API_TOKEN_ENV = "QWENPAW_DATA_API_TOKEN"
AUTH_EXEMPT_PATHS = frozenset({"/health"})


def get_configured_api_token() -> str:
    return (os.environ.get(API_TOKEN_ENV) or "").strip()


def extract_bearer_token(header: str | None) -> str:
    """Return a well-formed Bearer credential, otherwise an empty string."""
    if not header:
        return ""
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credential.strip()


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_api_token_auth(app: FastAPI) -> None:
    """Install the auth middleware.

    Call this *before* adding CORS (Starlette wraps later-added middleware
    outermost), so 401/403 responses still carry CORS headers.
    """

    @app.middleware("http")
    async def api_token_auth_middleware(request: Request, call_next):
        if request.url.path in AUTH_EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        expected = get_configured_api_token()
        if expected:
            credential = extract_bearer_token(
                request.headers.get("authorization"),
            )
            if not credential or not secrets.compare_digest(credential, expected):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "UNAUTHORIZED",
                        "message": "Missing or invalid API token",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

        client_host = request.client.host if request.client else None
        if not _is_loopback_host(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "code": "FORBIDDEN",
                    "message": (
                        "No API token configured; only loopback clients are "
                        f"accepted. Set {API_TOKEN_ENV} to serve remote clients."
                    ),
                },
            )
        return await call_next(request)
