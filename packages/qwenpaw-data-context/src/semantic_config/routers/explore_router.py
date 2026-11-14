"""mg-088 自动探查端点（§2.2）：GET/PUT explore-scope + POST explore（手动增量）。

路由前缀 ``/api/semantic-config/datasources``（复数；与单数 ``datasource`` 的
数据源 CRUD 路由并存不冲突）。v1 不暴露 MCP/host（裁决 Q8）。
"""

from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal
from fastapi import APIRouter, Depends, HTTPException, Request

from context_manager.api.dependencies import (
    get_context_runtime,
    get_context_authorization,
)
from context_manager.auth import WriteOwner
from semantic_config.db import get_db
from semantic_config.models.autoexplore import ExploreScopeUpdate
from semantic_config.principal import current_principal
from semantic_config.services import explore_service

router = APIRouter(prefix="/api/semantic-config/datasources", tags=["autoexplore"])


@router.get("/{datasource_id}/explore-scope")
async def get_explore_scope(
    datasource_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await explore_service.get_scope_view(db, datasource_id, principal)


@router.put("/{datasource_id}/explore-scope")
async def update_explore_scope(
    datasource_id: str,
    payload: ExploreScopeUpdate,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await explore_service.update_scope_view(
        db, datasource_id, principal, **payload.model_dump(exclude_unset=True)
    )


@router.post("/{datasource_id}/explore")
async def trigger_explore(
    datasource_id: str,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    runtime = get_context_runtime(request)
    try:
        scope = runtime.issue_graph_write_scope(
            get_context_authorization(),
            owner=WriteOwner.WORKSPACE,
            datasource_id=datasource_id,
            zone="metadata",
        )
    except PermissionError as exc:
        raise HTTPException(
            403, "physical discovery requires workspace administration"
        ) from exc
    graph_access = runtime.physical_mapping(scope), scope
    return await explore_service.manual_trigger(
        db, datasource_id, principal, graph_access=graph_access
    )
