"""Expose one unauthenticated dependency-aware Context service health route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from ..runtime import ContextRuntime, ContextRuntimeUnavailable
from ..store import StoreError


class HealthResponse(BaseModel):
    """Report only whether the configured Context runtime is usable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool


router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Check the one process-level ContextRuntime without selecting a user."""
    runtime = getattr(request.app.state, "context_runtime", None)
    if type(runtime) is not ContextRuntime or runtime.closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="context runtime is unavailable",
        )
    try:
        result = runtime.health()
    except (ContextRuntimeUnavailable, PermissionError, StoreError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="context runtime is unavailable",
        ) from exc
    if result.get("ok") is not True:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="context runtime is unavailable",
        )
    return HealthResponse(ok=True)


__all__ = ["HealthResponse", "health", "router"]
