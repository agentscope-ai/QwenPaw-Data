from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.dataset_column_meta import ColumnCreate, ColumnResponse, ColumnUpdate
from semantic_config.pagination import Page
from semantic_config.services import dataset_column_meta_service as service

router = APIRouter(prefix="/api/semantic-config/dataset-column-meta", tags=["dataset-column-meta"])


@router.post("", response_model=ColumnResponse)
async def create_column(payload: ColumnCreate, db: aiosqlite.Connection = Depends(get_db)):
    return await service.create(db, payload)


@router.put("/{col_id}", response_model=ColumnResponse)
async def update_column(col_id: int, payload: ColumnUpdate, db: aiosqlite.Connection = Depends(get_db)):
    return await service.update(db, col_id, payload)


@router.get("/dataset/{dataset_id}", response_model=list[ColumnResponse])
async def list_columns_by_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db)):
    return await service.list_by_dataset(db, dataset_id)


@router.get("/{col_id}", response_model=ColumnResponse)
async def get_column(col_id: int, db: aiosqlite.Connection = Depends(get_db)):
    return await service.get_one(db, col_id)


@router.get("", response_model=Page[ColumnResponse])
async def list_columns(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db),
):
    return await service.list_page(db, datasource_id, domain_id, dataset_id, page, size)


@router.delete("/{col_id}")
async def delete_column(col_id: int, db: aiosqlite.Connection = Depends(get_db)):
    return await service.delete(db, col_id)
