from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query, Request

from semantic_config.db import get_db
from semantic_config.models.weave_task import WeaveTaskCallback, WeaveTaskResponse, WeaveTaskSubmit
from semantic_config.pagination import Page
from semantic_config.services import weave_task_service as service

router = APIRouter(prefix="/api/semantic-config/weave-task", tags=["weave-task"])


@router.post("/submit", response_model=WeaveTaskResponse)
async def submit_task(
    payload: WeaveTaskSubmit,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    driver = getattr(request.app.state, "driver", None)
    governor = getattr(request.app.state, "blocking_io", None)
    return await service.submit(db, payload, driver=driver, governor=governor)


@router.get("", response_model=Page[WeaveTaskResponse])
async def list_task(
    datasource_name: str | None = Query(default=None),
    task_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_name, task_name, page, size)


@router.post("/{task_id}/kill", response_model=WeaveTaskResponse)
async def kill_task(task_id: str, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.kill(db, task_id)


@router.post("/callback", response_model=WeaveTaskResponse)
async def callback_task(payload: WeaveTaskCallback, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.callback(db, payload)
