"""Expose an authenticated current-user trigger for User Graph ingestion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ....user.loader import UserHistoryScope
from ...dependencies import AuthorizationDependency, RuntimeDependency


class ManualUserIngestResponse(BaseModel):
    """Report one bounded manual ingest attempt without exposing model data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["success", "partial_failed", "failed", "skipped"]
    reason: Literal["overlap"] | None = None
    users_discovered: int = Field(default=0, ge=0)
    users_succeeded: int = Field(default=0, ge=0)
    users_failed: int = Field(default=0, ge=0)
    users_unauthorized: int = Field(default=0, ge=0)
    messages_processed: int = Field(default=0, ge=0)
    messages_failed: int = Field(default=0, ge=0)
    sources: dict[str, int] = Field(default_factory=dict)
    details: list[dict[str, str]] = Field(default_factory=list)


async def trigger_user_ingest(
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> ManualUserIngestResponse:
    """Process pending history for the authenticated User in their Workspace."""
    adapter = runtime.user_ingest
    scope = UserHistoryScope(
        context.tenant_id,
        context.active_workspace_id,
        context.user_id,
    )
    try:
        result = await adapter.ingest(datetime.now(UTC), scope)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="manual User Graph ingest failed",
        ) from exc
    try:
        return ManualUserIngestResponse.model_validate(result)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="manual User Graph ingest returned an invalid result",
        ) from exc


__all__ = [
    "ManualUserIngestResponse",
    "trigger_user_ingest",
]
