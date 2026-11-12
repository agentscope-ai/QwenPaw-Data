from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.biz_domain import (
    BizDomainCreate,
    BizDomainResponse,
    BizDomainUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import biz_domain_service as service

router = APIRouter(prefix="/api/semantic-config/biz-domain", tags=["biz-domain"])


@router.post("", response_model=BizDomainResponse)
async def create_biz_domain(
    payload: BizDomainCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{domain_id}", response_model=BizDomainResponse)
async def update_biz_domain(
    domain_id: int,
    payload: BizDomainUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, domain_id, payload, principal)


@router.get("", response_model=Page[BizDomainResponse])
async def list_biz_domain(
    domain_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """按业务域名称过滤。业务域已与数据源解耦，无 datasource_id 过滤（SPEC 00 §11.2）。"""
    return await service.list_page(db, domain_name, page, size, principal)


@router.get("/{domain_id}", response_model=BizDomainResponse)
async def get_biz_domain(
    domain_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, domain_id, principal)


@router.delete("/{domain_id}")
async def delete_biz_domain(
    domain_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, domain_id, principal)
