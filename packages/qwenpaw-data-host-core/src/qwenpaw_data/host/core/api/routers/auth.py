# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from qwenpaw_data_auth import Principal

from qwenpaw_data.host.core.api.auth_middleware import auth_enabled, is_authenticated
from qwenpaw_data.host.core.api.deps import get_principal

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status")
async def status(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    """Public probe: the caller's real principal, or anonymous; never admin."""
    authenticated = is_authenticated()
    return {
        "enabled": auth_enabled(),
        "sso_enabled": False,
        "sso_login_path": "",
        "authenticated": authenticated,
        "principal": _principal_view(principal) if authenticated else None,
    }


@router.get("/me")
async def me(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    return _principal_view(principal)


def _principal_view(principal: Principal) -> dict[str, Any]:
    return {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
        "workspace_ids": list(principal.workspace_ids),
        "active_workspace_id": principal.active_workspace_id,
    }
