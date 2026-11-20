"""Provide trusted identity and Runtime to API handlers."""

from typing import Annotated

from qwenpaw_data_auth.authorization import AuthorizationContext
from fastapi import Depends, HTTPException, Request, status

from ..runtime import ContextRuntime
from ..auth import get_authorization


def get_context_runtime(request: Request) -> ContextRuntime:
    """Return the only process-level ContextRuntime bound to the application."""
    runtime = getattr(request.app.state, "context_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="context runtime is unavailable",
        )
    if type(runtime) is not ContextRuntime or runtime.closed:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="context runtime is misconfigured",
        )
    return runtime


def get_context_authorization() -> AuthorizationContext:
    """Expose the CM Auth-bound trusted Auth result to issuance dependencies."""
    try:
        context = get_authorization()
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authenticated Context authorization is required",
        ) from exc
    if type(context) is not AuthorizationContext:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Context authorization is misconfigured",
        )
    return context


async def require_context_runtime(request: Request) -> ContextRuntime:
    """Resolve the in-memory Runtime dependency without a thread handoff."""
    return get_context_runtime(request)


async def require_context_authorization() -> AuthorizationContext:
    """Read the request's trusted identity without a thread handoff."""
    return get_context_authorization()


RuntimeDependency = Annotated[ContextRuntime, Depends(require_context_runtime)]
AuthorizationDependency = Annotated[
    AuthorizationContext,
    Depends(require_context_authorization),
]


__all__ = [
    "AuthorizationDependency",
    "RuntimeDependency",
    "get_context_runtime",
    "get_context_authorization",
    "require_context_authorization",
    "require_context_runtime",
]
