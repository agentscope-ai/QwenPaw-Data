from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query, Request

from semantic_config.db import get_db
from semantic_config.models.datasource import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    DatasourceCreate,
    DatasourceResponse,
    DatasourceUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import datasource_service as service

router = APIRouter(prefix="/api/semantic-config/datasource", tags=["datasource"])


@router.post("", response_model=DatasourceResponse)
async def create_datasource(
    payload: DatasourceCreate,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.get("", response_model=Page[DatasourceResponse])
async def list_datasource(
    datasource_id: str | None = Query(default=None),
    datasource_name: str | None = Query(default=None),
    datasource_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(
        db, datasource_id, datasource_name, datasource_type, page, size, principal
    )


@router.post("/test-connection", response_model=ConnectionTestResponse)
async def test_connection(payload: ConnectionTestRequest):
    """存盘前测试连接：body 带 datasource_type + config。"""
    return await service.test_connection(payload.datasource_type, payload.config)


@router.get("/{datasource_id}", response_model=DatasourceResponse)
async def get_datasource(
    datasource_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.get_one(db, datasource_id, principal)


@router.put("/{datasource_id}", response_model=DatasourceResponse)
async def update_datasource(
    datasource_id: str,
    payload: DatasourceUpdate,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.update(db, datasource_id, payload, principal)


@router.post("/{datasource_id}/test-connection", response_model=ConnectionTestResponse)
async def test_connection_by_id(
    datasource_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    """测试已保存数据源的连接。"""
    return await service.test_connection_by_id(db, datasource_id, principal)


@router.delete("/{datasource_id}")
async def delete_datasource(
    datasource_id: str,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    driver = getattr(request.app.state, "driver", None)
    return await service.delete(db, datasource_id, principal, driver=driver)
