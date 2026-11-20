"""Bind trusted identity and validate its tenant/Workspace membership.

Auth authenticates the requester and supplies AuthorizationContext: user,
Workspace memberships and roles. This module binds that trusted result to the
request; GraphScopeAuthority decides which graph operations it permits.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from qwenpaw_data_auth.authorization import AuthorizationContext


class AuthorizationError(PermissionError):
    """Report a trusted Auth result that cannot authorize this Context target."""


authorization_ctx: ContextVar[AuthorizationContext | None] = ContextVar(
    "context_manager_authorization",
    default=None,
)


def get_authorization(*, required: bool = True) -> AuthorizationContext | None:
    """Return the trusted request authorization or fail closed."""
    context = authorization_ctx.get()
    if required and context is None:
        raise PermissionError("an authenticated authorization context is required")
    return context


@contextmanager
def bind_authorization(
    context: AuthorizationContext,
) -> Iterator[AuthorizationContext]:
    """Bind one trusted Auth result and restore the prior request context."""
    if type(context) is not AuthorizationContext:
        raise TypeError("context must be an AuthorizationContext")
    token: Token[AuthorizationContext | None] = authorization_ctx.set(context)
    try:
        yield context
    finally:
        authorization_ctx.reset(token)


def active_target(context: AuthorizationContext) -> tuple[str, str]:
    """Return the required active tenant/workspace from one trusted result."""
    if type(context) is not AuthorizationContext:
        raise TypeError("context must be an AuthorizationContext")
    if not isinstance(context.user_id, str) or not context.user_id:
        raise AuthorizationError("authenticated user is required")
    if not isinstance(context.tenant_id, str) or not context.tenant_id:
        raise AuthorizationError("authenticated tenant is required")
    if (
        not isinstance(context.active_workspace_id, str)
        or not context.active_workspace_id
    ):
        raise AuthorizationError("active workspace is required")
    return context.tenant_id, context.active_workspace_id


def require_target(
    context: AuthorizationContext,
    *,
    tenant_id: str,
    workspace_id: str,
) -> None:
    """Require tenant/Workspace membership, without choosing a graph owner."""
    active_tenant_id, active_workspace_id = active_target(context)
    if "platform_admin" in {context.role, context.active_workspace_role}:
        raise AuthorizationError(
            "platform administration does not grant tenant graph access"
        )
    if active_tenant_id != tenant_id:
        raise AuthorizationError(
            "authenticated tenant does not match this Context runtime"
        )
    if active_workspace_id != workspace_id:
        raise AuthorizationError("active workspace does not match this Context runtime")
    if workspace_id not in context.workspace_ids:
        raise AuthorizationError("active workspace is not authorized for this user")


__all__ = [
    "AuthorizationError",
    "active_target",
    "authorization_ctx",
    "bind_authorization",
    "get_authorization",
    "require_target",
]
