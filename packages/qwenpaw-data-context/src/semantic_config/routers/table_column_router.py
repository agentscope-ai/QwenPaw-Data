"""v2.1 P3 table_column 路由（由 dataset_column_meta_router 更名重构，③a）。

key=(datasource_id, table_name, column_name)；table_name 为两段全名（project.table /
schema.table，含 `.`），故按表列表走必填查询参数而非路径段（设计决策 D-R1）。
"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.table_column import (
    TableColumnCreate,
    TableColumnResponse,
    TableColumnUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import table_column_service as service

router = APIRouter(prefix="/api/semantic-config/table-column", tags=["table-column"])


@router.post("", response_model=TableColumnResponse)
async def create_column(
    payload: TableColumnCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{col_id}", response_model=TableColumnResponse)
async def update_column(
    col_id: int,
    payload: TableColumnUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, col_id, payload, principal)


@router.get("/table", response_model=list[TableColumnResponse])
async def list_columns_by_table(
    datasource_id: str = Query(...),
    table_name: str = Query(...),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """按两段全名列举一张物理表的全部列（datasource_id + table_name 均必填）。"""
    return await service.list_by_table(db, datasource_id, table_name, principal)


@router.get("/{col_id}", response_model=TableColumnResponse)
async def get_column(
    col_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, col_id, principal)


@router.get("", response_model=Page[TableColumnResponse])
async def list_columns(
    datasource_id: str | None = Query(default=None),
    table_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, table_name, page, size, principal)


@router.delete("/{col_id}")
async def delete_column(
    col_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, col_id, principal)
