from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from semantic_config.db import get_db
from semantic_config.models.biz_domain import BizDomainCreate, BizDomainResponse, BizDomainUpdate
from semantic_config.pagination import Page
from semantic_config.services import biz_domain_service as service

router = APIRouter(prefix="/api/semantic-config/biz-domain", tags=["biz-domain"])


@router.post("", response_model=BizDomainResponse)
async def create_biz_domain(payload: BizDomainCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.put("/{domain_id}", response_model=BizDomainResponse)
async def update_biz_domain(
    domain_id: int, payload: BizDomainUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")
):
    return await service.update(db, domain_id, payload)


@router.get("", response_model=Page[BizDomainResponse])
async def list_biz_domain(
    datasource_id: str | None = Query(default=None),
    domain_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, domain_name, page, size)


@router.get("/{domain_id}", response_model=BizDomainResponse)
async def get_biz_domain(domain_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, domain_id)


@router.delete("/{domain_id}")
async def delete_biz_domain(domain_id: int, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.delete(db, domain_id)
