"""探查路由：总览/状态（批次1）+ 6 阶段动作 + staging 行 CRUD + 上传 + 整库导入。

前缀 `/api/semantic-config/probe`，命中既有路由策略：GET 需 datasource:read（member 即可），
非 GET 需 datasource:write（仅管理员）；错误协议走 semantic-config 一侧。`{stage}` 路径段取
6 个：biz-domain/dataset/dimension-caliber/dimension-manage/metric-caliber/metric-manage。
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from qwenpaw_data_auth import Principal
from semantic_config.db import get_db
from semantic_config.errors import NotFoundNoBody
from semantic_config.pagination import Page
from semantic_config.principal import current_principal

from profiling.models.probe import (
    DatasourceStatusResponse,
    OverviewRecord,
    ProbeTaskResponse,
    RowIdsRequest,
)
from profiling.services import (
    orchestrator,
    overview_service,
    row_service,
    stage_service,
    upload_service,
)
from profiling.stages import StageDef, def_by_path

router = APIRouter(prefix="/api/semantic-config/probe", tags=["probe"])


def _resolve(stage: str) -> StageDef:
    sdef = def_by_path(stage)
    if sdef is None:
        raise NotFoundNoBody()
    return sdef


# ============================ 总览 / 状态 ============================
@router.get("/overview", response_model=Page[OverviewRecord])
async def probe_overview(
    datasource_id: str | None = Query(default=None),
    datasource_name: str | None = Query(default=None),
    datasource_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await overview_service.list_page(
        db, datasource_id, datasource_name, datasource_type, page, size, principal
    )


@router.get("/{datasource_id}/status", response_model=DatasourceStatusResponse)
async def probe_status(
    datasource_id: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await stage_service.get_status(db, datasource_id, principal)


# ============================ §7.4 整库一键导入 ============================
@router.post("/{datasource_id}/import/excel")
async def probe_import_excel(
    datasource_id: str,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    content = await file.read()
    runtime = getattr(request.app.state, "context_runtime", None)
    return await upload_service.import_all(
        db, datasource_id, file.filename or "", content, principal, runtime=runtime
    )


# ============================ 阶段动作 ============================
@router.post("/{datasource_id}/{stage}/start", response_model=ProbeTaskResponse)
async def stage_start(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await orchestrator.submit(
        db, datasource_id, sdef.stage, principal, mode="create"
    )


@router.post("/{datasource_id}/{stage}/rerun", response_model=ProbeTaskResponse)
async def stage_rerun(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await orchestrator.submit(
        db, datasource_id, sdef.stage, principal, mode="rerun"
    )


@router.post("/{datasource_id}/{stage}/refine", response_model=ProbeTaskResponse)
async def stage_refine(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    """完善探查：基于当前 staging 草稿补全；未实现 refine 的阶段由算法侧降级处理。"""
    sdef = _resolve(stage)
    return await orchestrator.submit(
        db, datasource_id, sdef.stage, principal, mode="refine"
    )


@router.post("/{datasource_id}/{stage}/stop")
async def stage_stop(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await orchestrator.stop(db, datasource_id, sdef.stage, principal)


@router.post("/{datasource_id}/{stage}/skip")
async def stage_skip(
    datasource_id: str,
    stage: str,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    runtime = getattr(request.app.state, "context_runtime", None)
    return await stage_service.skip(
        db, datasource_id, sdef.stage, principal, runtime=runtime
    )


@router.get("/{datasource_id}/{stage}/task", response_model=ProbeTaskResponse)
async def stage_task(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await orchestrator.get_task(db, datasource_id, sdef.stage, principal)


@router.post("/{datasource_id}/{stage}/complete")
async def stage_complete(
    datasource_id: str,
    stage: str,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    runtime = getattr(request.app.state, "context_runtime", None)
    return await stage_service.complete(
        db, datasource_id, sdef.stage, principal, runtime=runtime
    )


@router.get("/{datasource_id}/{stage}/results")
async def stage_results(
    datasource_id: str,
    stage: str,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    dimension_name: str | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    dataset_name: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    filter_val = dimension_name or metric_name or dataset_name
    return await stage_service.list_results(
        db, sdef, datasource_id, principal, page, size, filter_val
    )


@router.post("/{datasource_id}/{stage}/upload")
async def stage_upload(
    datasource_id: str,
    stage: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    content = await file.read()
    return await upload_service.upload_stage(
        db, sdef, datasource_id, file.filename or "", content, principal
    )


@router.get("/{datasource_id}/{stage}/export")
async def stage_export(
    datasource_id: str,
    stage: str,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    content = await upload_service.export_stage(db, sdef, datasource_id, principal)
    filename = f"profiling_{datasource_id}_{sdef.path}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================ staging 行级 CRUD + 批量 ============================
@router.post("/{datasource_id}/{stage}/row")
async def row_add(
    datasource_id: str,
    stage: str,
    body: dict = Body(default_factory=dict),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.add_row(db, sdef, datasource_id, body, principal)


@router.post("/{datasource_id}/{stage}/row/confirm")
async def row_confirm(
    datasource_id: str,
    stage: str,
    body: RowIdsRequest = Body(...),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.batch_confirm(
        db, sdef, datasource_id, body.row_ids, principal
    )


@router.post("/{datasource_id}/{stage}/row/skip")
async def row_skip(
    datasource_id: str,
    stage: str,
    body: RowIdsRequest = Body(...),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.batch_skip(
        db, sdef, datasource_id, body.row_ids, principal
    )


@router.post("/{datasource_id}/{stage}/row/delete")
async def row_batch_delete(
    datasource_id: str,
    stage: str,
    body: RowIdsRequest = Body(...),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.batch_delete(
        db, sdef, datasource_id, body.row_ids, principal
    )


@router.put("/{datasource_id}/{stage}/row/{row_id}")
async def row_update(
    datasource_id: str,
    stage: str,
    row_id: int,
    body: dict = Body(default_factory=dict),
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.update_row(
        db, sdef, datasource_id, row_id, body, principal
    )


@router.delete("/{datasource_id}/{stage}/row/{row_id}")
async def row_delete(
    datasource_id: str,
    stage: str,
    row_id: int,
    principal: Principal = Depends(current_principal),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    sdef = _resolve(stage)
    return await row_service.delete_row(db, sdef, datasource_id, row_id, principal)
