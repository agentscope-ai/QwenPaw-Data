"""v2.1 L8 verified_query 路由（新建表，黄金问答落点）。

domain_id/question/verified_sql 均必填（存储层三列皆 NOT NULL）。
唯一键 (tenant_id, workspace_id, domain_id, question)。
"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import get_db
from semantic_config.models.verified_query import (
    VerifiedQueryCreate,
    VerifiedQueryResponse,
    VerifiedQueryUpdate,
)
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import verified_query_service as service

router = APIRouter(
    prefix="/api/semantic-config/verified-query", tags=["verified-query"]
)


@router.post("", response_model=VerifiedQueryResponse)
async def create_verified_query(
    payload: VerifiedQueryCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.create(db, payload, principal)


@router.put("/{vq_id}", response_model=VerifiedQueryResponse)
async def update_verified_query(
    vq_id: int,
    payload: VerifiedQueryUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.update(db, vq_id, payload, principal)


@router.get("/{vq_id}", response_model=VerifiedQueryResponse)
async def get_verified_query(
    vq_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.get_one(db, vq_id, principal)


@router.get("", response_model=Page[VerifiedQueryResponse])
async def list_verified_query(
    domain_id: int | None = Query(default=None),
    question: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.list_page(db, domain_id, question, page, size, principal)


@router.delete("/{vq_id}")
async def delete_verified_query(
    vq_id: int,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await service.delete(db, vq_id, principal)
