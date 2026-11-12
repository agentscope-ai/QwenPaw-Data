"""Expose workspace embedding rebuilds through the frontend job protocol."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from ....auth import active_target
from ....embedding import EmbeddingJobManager, JobOwner, RebuildBusy
from ....embedding.service import (
    EmbeddingDimensionMismatch,
    public_job,
)
from ....model import EmbeddingSpaceConflict, ModelConfigError
from ....runtime import ContextRuntimeUnavailable
from ....store import StoreError
from ...dependencies import AuthorizationDependency, RuntimeDependency

router = APIRouter(prefix="/embed", tags=["context-manager-embedding"])


async def require_embedding_jobs(runtime: RuntimeDependency) -> EmbeddingJobManager:
    return runtime.embedding_jobs


JobsDependency = Annotated[EmbeddingJobManager, Depends(require_embedding_jobs)]


def _owner(context: AuthorizationDependency) -> JobOwner:
    return active_target(context)


def _error(status: int, code: str, message: str, **details: object) -> HTTPException:
    return HTTPException(
        status, detail={"error": {"code": code, "message": message, "details": details}}
    )


async def _start(
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    *,
    job_id: str | None = None,
) -> str:
    try:
        service = await runtime.embedding_rebuild(context)
        return await service.start(context, job_id=job_id)
    except RebuildBusy as exc:
        raise _error(
            409,
            "rebuild_already_active",
            "This workspace is already rebuilding its embeddings.",
            job_id=exc.job_id,
        ) from exc
    except EmbeddingSpaceConflict as exc:
        raise _error(409, "embedding_space_conflict", str(exc), **exc.details) from exc
    except EmbeddingDimensionMismatch as exc:
        raise _error(409, "embedding_dimension_mismatch", str(exc)) from exc
    except ModelConfigError as exc:
        raise _error(
            412,
            "model_config_required",
            "Save a complete embedding model and Host Provider before rebuilding.",
            field="embedding",
        ) from exc
    except PermissionError as exc:
        raise _error(
            403,
            "rebuild_forbidden",
            "Only a workspace administrator may rebuild embeddings.",
        ) from exc
    except ContextRuntimeUnavailable as exc:
        raise HTTPException(503, "context runtime is unavailable") from exc
    except BlockingIOError as exc:
        raise _error(503, "execution_busy", "Embedding execution is busy.") from exc
    except LookupError as exc:
        raise HTTPException(404, "embedding job not found") from exc
    except ValueError as exc:
        raise HTTPException(400, "only failed embedding jobs can be retried") from exc
    except StoreError as exc:
        if exc.code == "recovery_workspace_busy":
            raise _error(
                409, exc.code, "This workspace has unfinished recovery work."
            ) from exc
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(503, "embedding job storage is unavailable") from exc


@router.post("/embed-all")
async def rebuild_embeddings(
    runtime: RuntimeDependency, context: AuthorizationDependency
) -> dict:
    return {"job_id": await _start(runtime, context)}


@router.get("/jobs/latest")
async def get_latest_rebuild_job(
    jobs: JobsDependency, context: AuthorizationDependency
) -> dict | None:
    try:
        return public_job(await jobs.latest(_owner(context)))
    except SQLAlchemyError as exc:
        raise HTTPException(503, "embedding job storage is unavailable") from exc


@router.get("/jobs/{job_id}")
async def get_rebuild_job(
    job_id: str, jobs: JobsDependency, context: AuthorizationDependency
) -> dict:
    try:
        job = await jobs.get(_owner(context), job_id)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "embedding job storage is unavailable") from exc
    if job is None:
        raise HTTPException(404, "embedding job not found")
    return public_job(job)


@router.post("/jobs/{job_id}/retry")
async def retry_rebuild_job(
    job_id: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> dict:
    await _start(runtime, context, job_id=job_id)
    return {"status": "retrying", "job_id": job_id}


__all__ = ["router"]
