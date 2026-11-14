"""SPEC 01：数据集列 `/dataset-column-meta`；兼容旧前缀 `/dataset-field`。"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.dataset_field import (
    DatasetFieldCreate,
    DatasetFieldResponse,
    DatasetFieldUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import dataset_field_service as service


def _build(prefix: str, tags: list[str]) -> APIRouter:
    r = APIRouter(prefix=prefix, tags=tags)

    @r.post("", response_model=DatasetFieldResponse)
    async def create_field(
        payload: DatasetFieldCreate,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.create(db, payload, principal)

    @r.put("/{field_id}", response_model=DatasetFieldResponse)
    async def update_field(
        field_id: int,
        payload: DatasetFieldUpdate,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.update(db, field_id, payload, principal)

    @r.get("/dataset/{dataset_id}", response_model=list[DatasetFieldResponse])
    async def list_fields_by_dataset(
        dataset_id: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.list_by_dataset(db, dataset_id, principal)

    @r.get("/{field_id}", response_model=DatasetFieldResponse)
    async def get_field(
        field_id: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.get_one(db, field_id, principal)

    @r.get("", response_model=Page[DatasetFieldResponse])
    async def list_fields(
        datasource_id: str | None = Query(default=None),
        domain_id: int | None = Query(default=None),
        dataset_id: int | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1),
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.list_page(
            db,
            dataset_id,
            page,
            size,
            principal,
            datasource_id=datasource_id,
            domain_id=domain_id,
        )

    @r.delete("/{field_id}")
    async def delete_field(
        field_id: int,
        principal: Principal = Depends(current_principal),
        db: AsyncSession = Depends(get_db, scope="function"),
    ):
        return await service.delete(db, field_id, principal)

    return r


router = _build("/api/semantic-config/dataset-column-meta", ["dataset-column-meta"])
legacy_router = _build("/api/semantic-config/dataset-field", ["dataset-field"])
