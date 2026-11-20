from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.metric_lib import MetricCreate, MetricResponse, MetricUpdate
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import metric_lib_service as service

router = APIRouter(prefix="/api/semantic-config/metric-lib", tags=["metric-lib"])


@router.post("", response_model=MetricResponse)
async def create_metric(
    payload: MetricCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{metric_id}", response_model=MetricResponse)
async def update_metric(
    metric_id: int,
    payload: MetricUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, metric_id, payload, principal)


@router.get("/{metric_id}", response_model=MetricResponse)
async def get_metric(
    metric_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, metric_id, principal)


@router.get("", response_model=Page[MetricResponse])
async def list_metric(
    datasource_id: str | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """指标字典挂业务域。``datasource_id`` 是派生过滤（经域的 datasource_scope 收敛），
    非字典自身的列，供建模工作台单数据源视图使用（SPEC 00 §15.3）。"""
    return await service.list_page(
        db, domain_id, metric_name, page, size, principal, datasource_id=datasource_id
    )


@router.delete("/{metric_id}")
async def delete_metric(
    metric_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, metric_id, principal)
