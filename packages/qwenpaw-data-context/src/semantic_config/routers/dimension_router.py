from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.dimension import (
    DimensionCreate,
    DimensionResponse,
    DimensionUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import dimension_service as service

router = APIRouter(prefix="/api/semantic-config/dimension", tags=["dimension"])


@router.post("", response_model=DimensionResponse)
async def create_dimension(
    payload: DimensionCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{dim_id}", response_model=DimensionResponse)
async def update_dimension(
    dim_id: int,
    payload: DimensionUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, dim_id, payload, principal)


@router.get("", response_model=Page[DimensionResponse])
async def list_dimension(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    dimension_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """维度字典挂业务域。``datasource_id`` 是派生过滤（经域的 datasource_scope 收敛），
    非字典自身的列，供建模工作台单数据源视图使用（SPEC 00 §15.3）。"""
    return await service.list_page(
        db,
        domain_id,
        dimension_name,
        page,
        size,
        principal,
        datasource_id=datasource_id,
    )


@router.get("/{dim_id}", response_model=DimensionResponse)
async def get_dimension(
    dim_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, dim_id, principal)


@router.delete("/{dim_id}")
async def delete_dimension(
    dim_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, dim_id, principal)
