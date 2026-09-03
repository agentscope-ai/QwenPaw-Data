from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.dimension import DimensionCreate, DimensionResponse, DimensionUpdate
from semantic_config.pagination import Page
from semantic_config.services import dimension_service as service

router = APIRouter(prefix="/api/semantic-config/dimension", tags=["dimension"])


@router.post("", response_model=DimensionResponse)
async def create_dimension(payload: DimensionCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{dim_id}", response_model=DimensionResponse)
async def update_dimension(dim_id: int, payload: DimensionUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.update(db, dim_id, payload)


@router.get("", response_model=Page[DimensionResponse])
async def list_dimension(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dimension_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, domain_id, dimension_name, page, size)


@router.get("/{dim_id}", response_model=DimensionResponse)
async def get_dimension(dim_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, dim_id)


@router.delete("/{dim_id}")
async def delete_dimension(dim_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, dim_id)
