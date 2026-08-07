"""系统设置 — 模型配置 API。

路由前缀 ``/api/system/model-config``

接口：
- GET  /                                        读取配置（API Key 掩码）
- PUT  /llm                                     更新 LLM 配置
- POST /llm/test                                LLM 连接测试
- PUT  /embedding                               更新 Embedding 配置（rebuild_required 时自动触发重建）
- POST /embedding/test                          Embedding 连接测试
- GET  /embedding/jobs/latest                   最新重建任务状态
- GET  /embedding/jobs/{job_id}                 指定重建任务状态
- POST /embedding/jobs/{job_id}/retry           重试失败的重建任务
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..model_config_store import get_model_config_store

log = logging.getLogger("api.model_config")

router = APIRouter(prefix="/api/system/model-config", tags=["model-config"])


# ---- request / response models ----

class LLMConfigPayload(BaseModel):
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = Field(None, description="留空表示不修改")


class EmbeddingConfigPayload(BaseModel):
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(None, description="留空表示不修改")
    dim: Optional[int] = None


class TestResult(BaseModel):
    success: bool
    message: str
    detected_dim: Optional[int] = None


class EmbeddingUpdateResponse(BaseModel):
    embedding: dict[str, Any]
    rebuild_required: bool
    job_id: Optional[str] = None


class RebuildProgressResponse(BaseModel):
    phase: str = ""
    current_label: str = ""
    labels_done: int = 0
    labels_total: int = 0


class RebuildJobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    progress: RebuildProgressResponse = Field(default_factory=RebuildProgressResponse)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


# ---- endpoints ----

@router.get("/")
def get_model_config():
    store = get_model_config_store()
    return store.get_masked()


@router.put("/llm")
def update_llm_config(payload: LLMConfigPayload):
    store = get_model_config_store()
    masked = store.update_llm(payload.model_dump(exclude_none=False))
    return {"llm": masked}


@router.post("/llm/test")
def test_llm_connection(payload: LLMConfigPayload) -> TestResult:
    store = get_model_config_store()
    result = store.test_llm(payload.model_dump(exclude_none=True))
    return TestResult(**result)


@router.put("/embedding")
def update_embedding_config(
    payload: EmbeddingConfigPayload,
    request: Request,
) -> EmbeddingUpdateResponse:
    store = get_model_config_store()
    masked, rebuild = store.update_embedding(payload.model_dump(exclude_none=False))
    job_id = None
    if rebuild:
        from ..embedding_rebuild import get_rebuild_store, start_rebuild
        rebuild_store = get_rebuild_store()
        try:
            job = rebuild_store.create_job(config_snapshot={
                "model": payload.model,
                "base_url": payload.base_url,
                "dim": payload.dim,
            })
            job_id = job.job_id
            start_rebuild(request.app.state.driver, job.job_id)
        except RuntimeError as exc:
            log.warning("Rebuild job creation failed: %s", exc)
    return EmbeddingUpdateResponse(embedding=masked, rebuild_required=rebuild, job_id=job_id)


@router.post("/embedding/test")
def test_embedding_connection(payload: EmbeddingConfigPayload) -> TestResult:
    store = get_model_config_store()
    result = store.test_embedding(payload.model_dump(exclude_none=True))
    return TestResult(**result)


# ---- embedding rebuild job endpoints ----

def _job_to_response(job) -> RebuildJobResponse:
    from ..embedding_rebuild import RebuildProgress
    prog = job.progress if job.progress else RebuildProgress()
    return RebuildJobResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        progress=RebuildProgressResponse(
            phase=prog.phase,
            current_label=prog.current_label,
            labels_done=prog.labels_done,
            labels_total=prog.labels_total,
        ),
        config_snapshot=job.config_snapshot or {},
    )


@router.get("/embedding/jobs/latest")
def get_latest_rebuild_job():
    from ..embedding_rebuild import get_rebuild_store
    store = get_rebuild_store()
    job = store.get_latest_job()
    if not job:
        return None
    return _job_to_response(job)


@router.get("/embedding/jobs/{job_id}")
def get_rebuild_job(job_id: str):
    from ..embedding_rebuild import get_rebuild_store
    store = get_rebuild_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_to_response(job)


@router.post("/embedding/jobs/{job_id}/retry")
def retry_rebuild_job(job_id: str, request: Request):
    from ..embedding_rebuild import get_rebuild_store, start_rebuild
    store = get_rebuild_store()
    try:
        store.mark_retryable(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start_rebuild(request.app.state.driver, job_id)
    return {"status": "retrying", "job_id": job_id}
