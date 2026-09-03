from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.metric_lib import MetricCreate, MetricResponse, MetricUpdate
from semantic_config.pagination import Page
from semantic_config.services import metric_lib_service as service

router = APIRouter(prefix="/api/semantic-config/metric-lib", tags=["metric-lib"])


@router.post("", response_model=MetricResponse)
async def create_metric(payload: MetricCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{metric_id}", response_model=MetricResponse)
async def update_metric(metric_id: int, payload: MetricUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.update(db, metric_id, payload)


@router.get("/{metric_id}", response_model=MetricResponse)
async def get_metric(metric_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, metric_id)


@router.get("", response_model=Page[MetricResponse])
async def list_metric(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, domain_id, metric_name, page, size)


@router.delete("/{metric_id}")
async def delete_metric(metric_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, metric_id)
