"""Bind the qwenpaw-data-auth Principal consumed by aligned semantic routes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from qwenpaw_data_auth import Principal

ANONYMOUS_PRINCIPAL = Principal(
    user_id="",
    tenant_id="",
    workspace_ids=[],
    active_workspace_id="",
    role="anonymous",
)

principal_ctx: ContextVar[Principal] = ContextVar(
    "context_manager_qwenpaw_data_principal",
    default=ANONYMOUS_PRINCIPAL,
)


def auth_enabled() -> bool:
    """Return whether the current request has an authenticated host identity."""
    return principal_ctx.get() is not ANONYMOUS_PRINCIPAL


def get_principal() -> Principal:
    """Return the current qwenpaw-data-auth identity or an anonymous sentinel."""
    return principal_ctx.get()


@contextmanager
def bind_principal(principal: Principal) -> Iterator[Principal]:
    """Bind one host Principal and reliably restore the prior request context."""
    if type(principal) is not Principal:
        raise TypeError("principal must be a concrete qwenpaw-data-auth Principal")
    token: Token[Principal] = principal_ctx.set(principal)
    try:
        yield principal
    finally:
        principal_ctx.reset(token)


# TODO(cm-security-compat): remove this aligned-package Principal context after
# semantic_config and profiling consume Auth's canonical request context.


__all__ = [
    "ANONYMOUS_PRINCIPAL",
    "auth_enabled",
    "bind_principal",
    "get_principal",
    "principal_ctx",
]
