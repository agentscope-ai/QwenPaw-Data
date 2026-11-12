from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.dataset_meta import (
    DatasetMetaCreate,
    DatasetMetaResponse,
    DatasetMetaUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import dataset_meta_service as service

router = APIRouter(prefix="/api/semantic-config/dataset-meta", tags=["dataset-meta"])


@router.post("", response_model=DatasetMetaResponse)
async def create_dataset(
    payload: DatasetMetaCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{dataset_id}", response_model=DatasetMetaResponse)
async def update_dataset(
    dataset_id: int,
    payload: DatasetMetaUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, dataset_id, payload, principal)


@router.get("/{dataset_id}", response_model=DatasetMetaResponse)
async def get_dataset(
    dataset_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, dataset_id, principal)


@router.get("", response_model=Page[DatasetMetaResponse])
async def list_dataset(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dataset_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.list_page(
        db,
        datasource_id,
        domain_id,
        dataset_name,
        page,
        size,
        principal,
    )


@router.delete("/{dataset_id}")
async def delete_dataset(
    dataset_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, dataset_id, principal)
