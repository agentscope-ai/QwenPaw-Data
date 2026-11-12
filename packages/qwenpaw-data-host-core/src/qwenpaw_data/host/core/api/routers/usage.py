# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import raise_api
from qwenpaw_data.host.core.api.models.usage import (
    BackgroundTaskUsagePageSchema,
    SessionUsagePageSchema,
    UsageSummarySchema,
    UsageTimeseriesSchema,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.usage_window import (
    UsageWindow,
    UsageWindowError,
    resolve_usage_window,
)
from qwenpaw_data.host.core.repositories import UsageQueryRepository
from qwenpaw_data.host.core.utils.time import utcnow

router = APIRouter(prefix="/usage", tags=["usage"])

Period = Literal["1d", "7d", "30d"]


def usage_window(
    period: Period = Query("7d"),
    timezone: str | None = Query(None),
    as_of: datetime | None = Query(None),
) -> UsageWindow:
    try:
        return resolve_usage_window(
            period=period, timezone_name=timezone, as_of=as_of, now=utcnow()
        )
    except UsageWindowError as exc:
        raise_api(
            "VALIDATION",
            str(exc),
            status=400,
            details={"reason": exc.reason, "field": exc.field},
        )
        raise


@router.get("/summary", response_model=UsageSummarySchema)
async def get_usage_summary(
    window: UsageWindow = Depends(usage_window),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await UsageQueryRepository(db, identity).summary(window)


@router.get("/timeseries", response_model=UsageTimeseriesSchema)
async def get_usage_timeseries(
    window: UsageWindow = Depends(usage_window),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await UsageQueryRepository(db, identity).timeseries(window)


@router.get("/sessions", response_model=SessionUsagePageSchema)
async def get_usage_sessions(
    window: UsageWindow = Depends(usage_window),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await UsageQueryRepository(db, identity).sessions(
        window, page=page, page_size=page_size
    )


@router.get("/background-tasks", response_model=BackgroundTaskUsagePageSchema)
async def get_usage_background_tasks(
    window: UsageWindow = Depends(usage_window),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await UsageQueryRepository(db, identity).background_tasks(
        window, page=page, page_size=page_size
    )
