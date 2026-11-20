"""Forward Host's embedding job API to workspace rebuild operations."""

from fastapi import APIRouter, Response

from ..dependencies import AuthorizationDependency, RuntimeDependency
from ..native.embedding.views import (
    JobsDependency,
    get_latest_rebuild_job,
    get_rebuild_job,
    rebuild_embeddings,
    retry_rebuild_job,
)
from .config import _deprecate

# TODO(cm-api-compat): remove these forwarders after the frontend adopts
# /api/v1/embed/embed-all and /api/v1/embed/jobs/*.
router = APIRouter(
    prefix="/api/system/model-config/embedding", tags=["legacy-model-config"]
)


@router.post("/rebuild")
async def legacy_rebuild_embeddings(
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> dict:
    _deprecate(response, "/api/v1/embed/embed-all")
    return await rebuild_embeddings(runtime, context)


@router.get("/jobs/latest")
async def legacy_latest_rebuild_job(
    response: Response,
    jobs: JobsDependency,
    context: AuthorizationDependency,
) -> dict | None:
    _deprecate(response, "/api/v1/embed/jobs/latest")
    return await get_latest_rebuild_job(jobs, context)


@router.get("/jobs/{job_id}")
async def legacy_get_rebuild_job(
    job_id: str,
    response: Response,
    jobs: JobsDependency,
    context: AuthorizationDependency,
) -> dict:
    _deprecate(response, f"/api/v1/embed/jobs/{job_id}")
    return await get_rebuild_job(job_id, jobs, context)


@router.post("/jobs/{job_id}/retry")
async def legacy_retry_rebuild_job(
    job_id: str,
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> dict:
    _deprecate(response, f"/api/v1/embed/jobs/{job_id}/retry")
    return await retry_rebuild_job(job_id, runtime, context)
