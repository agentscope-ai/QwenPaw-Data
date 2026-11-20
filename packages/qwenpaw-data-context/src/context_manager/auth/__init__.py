"""Expose the Context Manager authorization boundary."""

from .authorization import (
    AuthorizationError,
    active_target,
    authorization_ctx,
    bind_authorization,
    get_authorization,
    require_target,
)
from .scope import AccessMode, GLOBAL_OWNER_ID, ScopeBinding, WriteOwner
from .scope_authority import GraphScopeAuthority, GraphScopeError

__all__ = [
    "AccessMode",
    "AuthorizationError",
    "active_target",
    "GLOBAL_OWNER_ID",
    "GraphScopeAuthority",
    "GraphScopeError",
    "ScopeBinding",
    "WriteOwner",
    "authorization_ctx",
    "bind_authorization",
    "get_authorization",
    "require_target",
]
