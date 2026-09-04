# -*- coding: utf-8 -*-
"""Wholesale plan replacement for live and idle chats."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.requests import PlanEditRequest
from qwenpaw_data.host.core.domain.chat import ACTIVE
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.utils.plan import plan_schema_to_sop, sop_plan_to_schema
from qwenpaw_data.host.core.utils.time import utcnow

router = APIRouter(
    prefix="/sessions/{session_id}/chats/{chat_id}/plan",
    tags=["plan"],
)


@router.post("/edit")
async def plan_edit(
    session_id: str,
    chat_id: str,
    body: PlanEditRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        await state.sessions.get(session_id)
        chat = await state.chats.get(chat_id, session_id=session_id)
        payload = None if body.plan is None else body.plan.model_dump(mode="json")
        sop = plan_schema_to_sop(payload, previous=chat.plan)

        if chat.status in ACTIVE:
            runtime = get_runtime_registry().get(chat_id)
            if runtime is None:
                raise RuntimeError(
                    "CONFLICT: active chat runtime is unavailable",
                )
            if sop is None or not sop["nodes"]:
                raise_api(
                    "VALIDATION",
                    "a running chat requires a non-empty plan",
                    status=400,
                )
            snapshot = await runtime.replace_plan(sop, reason=body.reason)
        else:
            snapshot = sop or {}
            chat.plan = snapshot or None
            chat.updated_at = utcnow()
            await state.chats.save(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"plan": sop_plan_to_schema(snapshot)}
