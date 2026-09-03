from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.dataset_meta import DatasetMetaCreate, DatasetMetaResponse, DatasetMetaUpdate
from semantic_config.pagination import Page
from semantic_config.services import dataset_meta_service as service

router = APIRouter(prefix="/api/semantic-config/dataset-meta", tags=["dataset-meta"])


@router.post("", response_model=DatasetMetaResponse)
async def create_dataset(payload: DatasetMetaCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{dataset_id}", response_model=DatasetMetaResponse)
async def update_dataset(
    dataset_id: int, payload: DatasetMetaUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")
):
    return await service.update(db, dataset_id, payload)


@router.get("/{dataset_id}", response_model=DatasetMetaResponse)
async def get_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, dataset_id)


@router.get("", response_model=Page[DatasetMetaResponse])
async def list_dataset(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dataset_name: str | None = Query(default=None),
    dataset_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, domain_id, dataset_name, dataset_type, page, size)


@router.delete("/{dataset_id}")
async def delete_dataset(dataset_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, dataset_id)
