# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.domain.identity import Identity

router = APIRouter(prefix="/cron", tags=["cron"])


def _map(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise exc


async def _check_session(state: ServiceState, session_id: str | None) -> None:
    if session_id:
        await state.sessions.get(session_id)


async def _check_channel(
    state: ServiceState, identity: Identity, body: CronJobWrite
) -> None:
    """IM jobs must target a known channel target (the bot has spoken to it)."""
    if body.channel == "console":
        return
    manager = state.channel_manager
    if manager is None:
        raise ValueError(f"channel {body.channel!r} is not running")
    await manager.is_channel_eligible_for_cron_job(identity, body)


@router.get("/jobs")
async def list_jobs(
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    jobs = await state.cron.list(identity.user_id)
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/jobs")
async def create_job(
    body: CronJobWrite,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await _check_session(state, body.session_id)
        await _check_channel(state, identity, body)
        job = await state.cron.create(identity.user_id, body)
        state.cron_manager.sync(job)
        return {"job": job}
    except Exception as exc:
        _map(exc)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        return {"job": await state.cron.get(identity.user_id, job_id)}
    except Exception as exc:
        _map(exc)


@router.put("/jobs/{job_id}")
async def replace_job(
    job_id: str,
    body: CronJobWrite,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await _check_session(state, body.session_id)
        await _check_channel(state, identity, body)
        job = await state.cron.replace(identity.user_id, job_id, body)
        state.cron_manager.sync(job)
        return {"job": job}
    except Exception as exc:
        _map(exc)


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await state.cron.delete(identity.user_id, job_id)
        state.cron_manager.remove(job_id)
        return {"ok": True}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/pause")
async def pause_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        job = await state.cron.set_enabled(identity.user_id, job_id, False)
        state.cron_manager.sync(job)
        return {"job": job}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        job = await state.cron.set_enabled(identity.user_id, job_id, True)
        state.cron_manager.sync(job)
        return {"job": job}
    except Exception as exc:
        _map(exc)


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    """Fire the job immediately in the background."""
    try:
        job = await state.cron.get(identity.user_id, job_id)
    except Exception as exc:
        _map(exc)
    state.track(asyncio.create_task(state.cron_manager.run(job)))
    return {"ok": True}


@router.get("/targets")
async def list_targets(
    channel: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    """Known IM send targets for this user and channel (cron target picker)."""
    try:
        targets = await state.channel_bindings.list_by_channel(
            identity.user_id, channel
        )
        return {"targets": targets, "count": len(targets)}
    except Exception as exc:
        _map(exc)
