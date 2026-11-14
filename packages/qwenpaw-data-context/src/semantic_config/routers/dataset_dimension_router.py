from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.dataset_dimension import (
    DatasetDimensionCreate,
    DatasetDimensionResponse,
    DatasetDimensionUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import dataset_dimension_service as service

router = APIRouter(
    prefix="/api/semantic-config/dataset-dimension", tags=["dataset-dimension"]
)


@router.post("", response_model=DatasetDimensionResponse)
async def create_binding(
    payload: DatasetDimensionCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{dd_id}", response_model=DatasetDimensionResponse)
async def update_binding(
    dd_id: int,
    payload: DatasetDimensionUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, dd_id, payload, principal)


@router.get("/dataset/{dataset_id}", response_model=list[DatasetDimensionResponse])
async def list_by_dataset(
    dataset_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.list_by_dataset(db, dataset_id, principal)


@router.get("/{dd_id}", response_model=DatasetDimensionResponse)
async def get_binding(
    dd_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, dd_id, principal)


@router.get("", response_model=Page[DatasetDimensionResponse])
async def list_binding(
    dataset_id: int | None = Query(default=None),
    dimension_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """v2.1：绑定去数据源绑定；datasource_id/domain_id/dataset_name 过滤退役，只留 dataset_id/dimension_name。"""
    return await service.list_page(
        db, dataset_id, dimension_name, page, size, principal
    )


@router.delete("/dataset/{dataset_id}")
async def delete_bindings_by_dataset(
    dataset_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete_by_dataset(db, dataset_id, principal)


@router.delete("/{dd_id}")
async def delete_binding(
    dd_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, dd_id, principal)
