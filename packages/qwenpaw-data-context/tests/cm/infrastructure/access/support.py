"""Build trusted Auth results for Context Manager capability tests."""

from __future__ import annotations

from qwenpaw_data_auth.authorization import AuthorizationContext


def authorization(
    user_id: str = "user-1",
    tenant_id: str = "tenant-1",
    workspace_id: str = "workspace-1",
    *,
    role: str = "member",
    tenant_role: str | None = None,
) -> AuthorizationContext:
    """Return one exact active-Workspace authorization context."""
    active_role = role
    return AuthorizationContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=tenant_role or active_role,
        active_workspace_role=active_role,
        workspace_ids=frozenset({workspace_id}),
        admin_workspace_ids=(
            frozenset({workspace_id})
            if active_role in {"tenant_admin", "ws_admin"}
            else frozenset()
        ),
        active_workspace_id=workspace_id,
    )


__all__ = ["authorization"]
