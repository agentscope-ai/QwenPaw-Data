from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, Query, Request

from context_manager.api.native.metadata.weaving import publisher_for_request
from semantic_config.db import get_db
from semantic_config.models.weave_task import WeaveTaskResponse, WeaveTaskSubmit
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import weave_task_service as service

router = APIRouter(prefix="/api/semantic-config/weave-task", tags=["weave-task"])


@router.post("/submit", response_model=WeaveTaskResponse)
async def submit_task(
    payload: WeaveTaskSubmit,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    publisher, scope = publisher_for_request(request)
    return await publisher.submit_task(db, payload, principal, scope)


@router.get("", response_model=Page[WeaveTaskResponse])
async def list_task(
    datasource_name: str | None = Query(default=None),
    task_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(
        db, datasource_name, task_name, page, size, principal
    )


@router.get("/{task_id}", response_model=WeaveTaskResponse)
async def get_task(
    task_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.get(db, task_id, principal)


@router.post("/{task_id}/kill", response_model=WeaveTaskResponse)
async def kill_task(
    task_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.kill(db, task_id, principal)
