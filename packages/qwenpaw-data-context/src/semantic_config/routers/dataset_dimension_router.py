from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.dataset_dimension import (
    DatasetDimensionCreate,
    DatasetDimensionResponse,
    DatasetDimensionUpdate,
)
from semantic_config.pagination import Page
from semantic_config.services import dataset_dimension_service as service

router = APIRouter(prefix="/api/semantic-config/dataset-dimension", tags=["dataset-dimension"])


@router.post("", response_model=DatasetDimensionResponse)
async def create_binding(payload: DatasetDimensionCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{dd_id}", response_model=DatasetDimensionResponse)
async def update_binding(
    dd_id: int, payload: DatasetDimensionUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")
):
    return await service.update(db, dd_id, payload)


@router.get("/dataset/{dataset_id}", response_model=list[DatasetDimensionResponse])
async def list_by_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.list_by_dataset(db, dataset_id)


@router.get("/{dd_id}", response_model=DatasetDimensionResponse)
async def get_binding(dd_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, dd_id)


@router.get("", response_model=Page[DatasetDimensionResponse])
async def list_binding(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    dataset_name: str | None = Query(default=None),
    dimension_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(
        db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name, page, size
    )


@router.delete("/dataset/{dataset_id}")
async def delete_bindings_by_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete_by_dataset(db, dataset_id)


@router.delete("/{dd_id}")
async def delete_binding(dd_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, dd_id)
