# -*- coding: utf-8 -*-
"""Delegated access for non-HTTP execution (cron / IM / settlement)."""

from __future__ import annotations

import os

import httpx

from qwenpaw_data.host.core.api.auth_middleware import auth_enabled
from qwenpaw_data.host.core.domain.identity import Identity


def generate_temporary_token(
    identity: Identity, *, ttl: int | None = None
) -> str | None:
    """Auth off → None; auth on → Auth POST /api/auth/temporary-access."""
    if not auth_enabled():
        return None
    if ttl is not None and ttl <= 0:
        raise ValueError("ttl must be positive")

    base = os.environ.get("QWENPAW_DATA_AUTH_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("QWENPAW_DATA_AUTH_URL is required when auth is enabled")
    secret = os.environ.get("QWENPAW_DATA_SERVICE_SECRET", "").strip()
    if not secret:
        raise RuntimeError("QWENPAW_DATA_SERVICE_SECRET is required when auth is enabled")

    payload: dict[str, object] = {
        "user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
        "workspace_id": identity.workspace_id,
    }
    if ttl is not None:
        payload["ttl"] = ttl

    try:
        response = httpx.post(
            f"{base}/api/auth/temporary-access",
            headers={"X-QwenPaw-Data-Service-Secret": secret},
            json=payload,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"auth temporary-access request failed: {exc}") from exc

    if response.status_code == 404:
        detail = _response_detail(response) or "user not found or inactive"
        raise LookupError(detail)
    if response.status_code == 403:
        detail = _response_detail(response) or "forbidden"
        raise PermissionError(detail)
    if response.status_code >= 400:
        detail = _response_detail(response) or response.text
        raise RuntimeError(
            f"auth temporary-access returned HTTP {response.status_code}: {detail}"
        )

    token = response.json().get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("auth temporary-access response missing access_token")
    return token


def _response_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return ""
    return str(detail) if detail else ""
