"""TG (Trace Graph) management API — lifecycle, Claim editing, Strategy Card ops.

Prefix: ``/api/v1/tg``
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Query, Request

from ..utils import get_logger
from . import tg_admin_store as store
from .response_envelope import clamp_page, fail, paginated, success
from .tg_admin_models import (
    BatchTaskKeysRequest,
    InvalidateRequest,
    UpdateClaimFieldsRequest,
    UpdateStrategyFieldsRequest,
    UpdateTaskStatusRequest,
)

log = get_logger("api.tg_admin")

router = APIRouter(prefix="/api/v1/admin/tg", tags=["tg-admin"])


def _driver(request: Request):
    """Extract the Neo4j driver from app state."""
    return request.app.state.driver


def _success_after_write(result):
    """Wrap a successful graph mutation, dropping stale global snapshots."""
    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return success(result)


# ═══════════════════════════════════════════════════════════════════════ #
#  Task
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated Task listing with status, date range, and keyword filters."""
    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = store.list_tasks(
        driver, status=status, date_from=date_from, date_to=date_to,
        q=q, page=page, page_size=page_size,
    )
    return paginated(rows, total=total, page=page, page_size=page_size)


@router.get("/tasks/{key:path}/graph")
def get_task_graph(key: str, request: Request):
    """Return the full trace subgraph for a single Task."""
    from .trace_api import get_trace_graph
    return get_trace_graph(key, request)


@router.patch("/tasks/{key:path}/status")
def update_task_status(key: str, body: UpdateTaskStatusRequest, request: Request):
    """Transition a Task's status with state-machine validation."""
    driver = _driver(request)
    try:
        result = store.update_task_status(
            driver, key, new_status=body.status, reason=body.reason,
        )
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/tasks/batch-archive")
def batch_archive_tasks(body: BatchTaskKeysRequest, request: Request):
    """Archive up to 50 Tasks in one call."""
    from ..graph.trace import batch_archive_tasks

    driver = _driver(request)
    if len(body.task_keys) > 50:
        fail("VALIDATION_ERROR", "单次最多归档 50 个 Task")
    try:
        result = batch_archive_tasks(driver, body.task_keys, reason=body.reason or "")
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.delete("/tasks/{key:path}")
def delete_task(key: str, request: Request):
    """Cascade-delete a Task and all its downstream nodes."""
    from ..graph.trace import delete_task_cascade

    driver = _driver(request)
    try:
        result = delete_task_cascade(driver, key)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/tasks/batch-delete")
def batch_delete_tasks(body: BatchTaskKeysRequest, request: Request):
    """Cascade-delete up to 50 Tasks in one call."""
    from ..graph.trace import batch_delete_tasks

    driver = _driver(request)
    if len(body.task_keys) > 50:
        fail("VALIDATION_ERROR", "单次最多删除 50 个 Task")
    try:
        result = batch_delete_tasks(driver, body.task_keys)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


# ═══════════════════════════════════════════════════════════════════════ #
#  Claim
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/claims")
def list_claims_global(
    request: Request,
    subject_type: str | None = None,
    q: str | None = None,
    valid: bool | None = None,
    task_key: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated global Claim listing with multi-dimension filters."""
    from ..graph.trace import list_claims_global

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = list_claims_global(
        driver, subject_type=subject_type, q=q, valid=valid,
        task_key=task_key, page=page, page_size=page_size,
    )
    return paginated(rows, total=total, page=page, page_size=page_size)


@router.get("/tasks/{key:path}/claims")
def list_task_claims(
    key: str,
    request: Request,
    page: int = 1,
    page_size: int = 50,
):
    """List Claims produced by a specific Task."""
    from ..graph.trace import list_claims_global

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = list_claims_global(driver, task_key=key, page=page, page_size=page_size)
    return paginated(rows, total=total, page=page, page_size=page_size)


@router.patch("/claims/{key:path}")
def update_claim(key: str, body: UpdateClaimFieldsRequest, request: Request, bg: BackgroundTasks):
    """Partially update whitelisted fields on a Claim."""
    driver = _driver(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        fail("VALIDATION_ERROR", "至少提供一个要编辑的字段")
    try:
        result = store.update_claim_fields(driver, key, updates=updates, background_tasks=bg)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/claims/{key:path}/invalidate")
def invalidate_claim(key: str, body: InvalidateRequest, request: Request):
    """Mark a Claim as invalid with a mandatory reason."""
    from ..graph.trace import invalidate_claim

    driver = _driver(request)
    try:
        result = invalidate_claim(driver, key, reason=body.reason)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


# ═══════════════════════════════════════════════════════════════════════ #
#  Strategy Card
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/strategies")
def list_strategies(
    request: Request,
    polarity: str | None = None,
    memory_tier: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated Strategy Card listing with polarity and tier filters."""
    from ..graph.strategy_card import list_strategy_cards

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = list_strategy_cards(
        driver, polarity=polarity, memory_tier=memory_tier,
        page=page, page_size=page_size,
    )
    return paginated(rows, total=total, page=page, page_size=page_size)


@router.get("/strategies/{key:path}")
def get_strategy_detail(key: str, request: Request):
    """Return a Strategy Card with related tasks and hit records."""
    from ..graph.strategy_card import get_strategy_card_detail

    driver = _driver(request)
    detail = get_strategy_card_detail(driver, key)
    if not detail:
        fail("NOT_FOUND", f"Strategy card not found: {key}", status_code=404)
    return success(detail)


@router.patch("/strategies/{key:path}")
def update_strategy(key: str, body: UpdateStrategyFieldsRequest, request: Request):
    """Partially update whitelisted fields on a Strategy Card."""
    driver = _driver(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        fail("VALIDATION_ERROR", "至少提供一个要编辑的字段")
    try:
        result = store.update_strategy_card_fields(driver, key, updates=updates)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


@router.post("/strategies/{key:path}/invalidate")
def invalidate_strategy(key: str, body: InvalidateRequest, request: Request):
    """Expire a Strategy Card with a mandatory reason."""
    from ..graph.strategy_card import invalidate_strategy_card

    driver = _driver(request)
    try:
        result = invalidate_strategy_card(driver, key, reason=body.reason)
        return _success_after_write(result)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))


# ═══════════════════════════════════════════════════════════════════════ #
#  Experience (read-only)
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/experiences")
def list_experiences(
    request: Request,
    task_signature: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """Paginated Experience listing with task_signature and keyword filters."""
    from .retrieval import list_experiences as _list_experiences

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = _list_experiences(
        driver, task_signature=task_signature, q=q, page=page, page_size=page_size,
    )
    return paginated(rows, total=total, page=page, page_size=page_size)


@router.get("/experiences/{key:path}")
def get_experience(key: str, request: Request):
    """Return an Experience's full properties and derived-from Tasks."""
    from .retrieval import get_experience_detail

    driver = _driver(request)
    detail = get_experience_detail(driver, key)
    if not detail:
        fail("NOT_FOUND", f"Experience not found: {key}", status_code=404)
    return success(detail)


# ═══════════════════════════════════════════════════════════════════════ #
#  Tag (read-only)
# ═══════════════════════════════════════════════════════════════════════ #

@router.get("/tags")
def list_tags(
    request: Request,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    """Paginated Tag listing with per-tag usage count."""
    from .retrieval import list_tags as _list_tags

    driver = _driver(request)
    page, page_size = clamp_page(page, page_size)
    rows, total = _list_tags(driver, category=category, q=q, page=page, page_size=page_size)
    return paginated(rows, total=total, page=page, page_size=page_size)
