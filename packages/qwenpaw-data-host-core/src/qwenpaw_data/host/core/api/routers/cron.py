# -*- coding: utf-8 -*-
"""Cron jobs HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.cron import check_session_exists
from qwenpaw_data.host.core.repositories.channel_binding import ChannelBindingRepository
from qwenpaw_data.host.core.repositories.cron import CronJobRepository

router = APIRouter(prefix="/cron", tags=["cron"])


def _map(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise


async def _ensure_can_write(
    db: AsyncSession, identity: Identity, body: CronJobWrite
) -> None:
    await check_session_exists(db, identity, body.session_id, body.channel)


@router.get("/targets")
async def list_targets(
    channel: str = Query(..., description="channel key, e.g. feishu"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Groups/people this user's bot has talked to, for a later frontend picker. Does not expose send_meta."""
    targets = await ChannelBindingRepository(db).list_by_channel(identity, str(channel))
    public = [
        {
            "external_key": t["external_key"],
            "target_type": t["target_type"],
            "display_name": t["display_name"],
            "last_active_at": t["last_active_at"],
        }
        for t in targets
    ]
    return {"targets": public, "count": len(public)}


@router.get("/jobs")
async def list_jobs(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    keyword: str | None = None,
    enabled: bool | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    jobs = await CronJobRepository(db, identity).list(
        keyword=keyword,
        enabled=enabled,
        channel=channel,
    )
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/jobs")
async def create_job(
    body: CronJobWrite,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await _ensure_can_write(db, identity, body)
        return {"job": await CronJobRepository(db, identity).create(body)}
    except Exception as exc:
        _map(exc)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return {"job": await CronJobRepository(db, identity).get(job_id)}
    except Exception as exc:
        _map(exc)


@router.put("/jobs/{job_id}")
async def replace_job(
    job_id: str,
    body: CronJobWrite,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await _ensure_can_write(db, identity, body)
        return {"job": await CronJobRepository(db, identity).replace(job_id, body)}
    except Exception as exc:
        _map(exc)


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await CronJobRepository(db, identity).delete(job_id)
        return {"ok": True}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/pause")
async def pause_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return {"job": await CronJobRepository(db, identity).set_enabled(job_id, False)}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return {"job": await CronJobRepository(db, identity).set_enabled(job_id, True)}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "job": await CronJobRepository(db, identity).run_now(job_id),
        }
    except Exception as exc:
        _map(exc)
