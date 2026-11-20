"""Pull personal MG after a user gains access to new Workspaces."""

from __future__ import annotations

import logging

import httpx

from qwenpaw_data_auth.config import AuthConfig

logger = logging.getLogger(__name__)


async def sync_user_workspaces(
    cfg: AuthConfig, tenant_id: str, user_id: str, workspace_ids: set[str]
) -> None:
    """Attempt each new Workspace once after the membership transaction commits."""
    for workspace_id in sorted(workspace_ids):
        if not cfg.service_tokens_enabled or not cfg.cm_base_url:
            logger.warning(
                "Initial MG sync skipped for tenant=%s workspace=%s user=%s: "
                "QWENPAW_DATA_CM_BASE_URL and service tokens are required",
                tenant_id,
                workspace_id,
                user_id,
            )
            continue
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if cfg.auth_url:
                    token_response = await client.post(
                        f"{cfg.auth_url}/api/auth/temporary-access",
                        headers={"X-QwenPaw-Data-Service-Secret": cfg.service_secret},
                        json={
                            "tenant_id": tenant_id,
                            "workspace_id": workspace_id,
                            "user_id": user_id,
                        },
                    )
                    token_response.raise_for_status()
                    token = token_response.json()["access_token"]
                else:
                    # Embedded local Auth has no external URL; still resolve
                    # current RBAC rather than trusting the caller's role.
                    from qwenpaw_data_auth.authentication import (
                        resolve_current_authorization,
                    )
                    from qwenpaw_data_auth.token import TokenIssuer

                    _, role, _ = resolve_current_authorization(
                        cfg.control_plane_dsn, user_id, tenant_id, workspace_id
                    )
                    token = TokenIssuer(cfg.secret_keys[0]).issue_access(
                        user_id, tenant_id, workspace_id, role, cfg.access_token_ttl
                    )
                response = await client.post(
                    f"{cfg.cm_base_url}/api/v1/mg/userspace/sync",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(2400.0, connect=10.0),
                )
                response.raise_for_status()
            logger.info(
                "Initial MG sync completed for tenant=%s workspace=%s user=%s",
                tenant_id,
                workspace_id,
                user_id,
            )
        except Exception:
            # Access is committed; failure must not undo it or skip other Workspaces.
            # HTTP exceptions may include sensitive headers; report scope only.
            logger.warning(
                "Initial MG sync failed for tenant=%s workspace=%s user=%s",
                tenant_id,
                workspace_id,
                user_id,
            )
