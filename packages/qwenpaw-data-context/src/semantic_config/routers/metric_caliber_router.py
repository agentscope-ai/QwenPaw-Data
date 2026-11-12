"""SPEC 01：指标口径 `/metric-formula-lib`；兼容旧前缀 `/metric-caliber`。"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.metric_caliber import (
    CaliberCreate,
    CaliberResponse,
    CaliberUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import metric_caliber_service as service


def _build(prefix: str, tags: list[str]) -> APIRouter:
    r = APIRouter(prefix=prefix, tags=tags)

    @r.post("", response_model=CaliberResponse)
    async def create_caliber(
        payload: CaliberCreate,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.create(db, payload, principal)

    @r.put("/{cid}", response_model=CaliberResponse)
    async def update_caliber(
        cid: int,
        payload: CaliberUpdate,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.update(db, cid, payload, principal)

    @r.get("/{cid}", response_model=CaliberResponse)
    async def get_caliber(
        cid: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.get_one(db, cid, principal)

    @r.get("", response_model=Page[CaliberResponse])
    async def list_caliber(
        metric_id: int | None = Query(default=None),
        dataset_id: int | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1),
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.list_page(db, metric_id, dataset_id, page, size, principal)

    @r.delete("/metric/{metric_id}")
    async def delete_calibers_by_metric(
        metric_id: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.delete_by_metric(db, metric_id, principal)

    @r.delete("/{cid}")
    async def delete_caliber(
        cid: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.delete(db, cid, principal)

    return r


router = _build("/api/semantic-config/metric-formula-lib", ["metric-formula-lib"])
legacy_router = _build("/api/semantic-config/metric-caliber", ["metric-caliber"])
