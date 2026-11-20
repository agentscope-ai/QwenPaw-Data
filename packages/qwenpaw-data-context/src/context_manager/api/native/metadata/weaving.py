"""Bind semantic-config HTTP operations to authoritative Metadata weaving."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from semantic_config.db import get_db
from semantic_config.errors import BadRequestError
from semantic_config.models.weave_task import WeaveTaskResponse, WeaveTaskSubmit
from semantic_config.pagination import Page
from semantic_config.principal import current_principal
from semantic_config.services import weave_task_service

from ....auth import WriteOwner
from ....metadata.weaving.integration import install_publication
from ...dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
    get_context_authorization,
    get_context_runtime,
)

router = APIRouter(prefix="/api/semantic-config", tags=["semantic-weaving"])


def workspace_scope(runtime, context):
    try:
        return runtime.issue_graph_write_scope(
            context, owner=WriteOwner.WORKSPACE, zone="metadata"
        )
    except PermissionError as exc:
        raise HTTPException(
            403, "authoritative Metadata writes require workspace administration"
        ) from exc


def publisher_for_request(request: Request):
    runtime = get_context_runtime(request)
    scope = workspace_scope(runtime, get_context_authorization())
    return runtime.metadata_weaving(scope), scope


@router.post("/weave-task/submit", response_model=WeaveTaskResponse)
async def submit_task(
    payload: WeaveTaskSubmit,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    principal=Depends(current_principal),
    db=Depends(get_db, scope="function"),
):
    scope = workspace_scope(runtime, context)
    service = runtime.metadata_weaving(scope)
    return await service.submit_task(db, payload, principal, scope)


@router.get("/weave-task", response_model=Page[WeaveTaskResponse])
async def list_tasks(
    datasource_name: str | None = None,
    task_name: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1),
    principal=Depends(current_principal),
    db=Depends(get_db, scope="function"),
):
    return await weave_task_service.list_page(
        db, datasource_name, task_name, page, size, principal
    )


@router.get("/weave-task/{task_id}", response_model=WeaveTaskResponse)
async def get_task(
    task_id: str,
    principal=Depends(current_principal),
    db=Depends(get_db, scope="function"),
):
    return await weave_task_service.get(db, task_id, principal)


@router.post("/weave-task/{task_id}/kill", response_model=WeaveTaskResponse)
async def kill_task(
    task_id: str,
    principal=Depends(current_principal),
    db=Depends(get_db, scope="function"),
):
    return await weave_task_service.kill(db, task_id, principal)


@router.post("/import/excel")
async def import_excel(
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    file: UploadFile | None = File(None),
    datasource_id: str | None = Form(None),
    principal=Depends(current_principal),
    db=Depends(get_db, scope="function"),
):
    if file is None:
        raise BadRequestError("上传文件不能为空")
    if not datasource_id or not datasource_id.strip():
        raise BadRequestError("datasource_id 不能为空")
    scope = workspace_scope(runtime, context)
    return await runtime.metadata_weaving(scope).import_excel(
        db,
        file.filename or "",
        await file.read(),
        principal,
        scope,
        datasource_id=datasource_id.strip(),
    )


def install_weaving(app):
    """Bind Metadata publication to the application's editor requests."""
    install_publication(app, publisher_for_request)
