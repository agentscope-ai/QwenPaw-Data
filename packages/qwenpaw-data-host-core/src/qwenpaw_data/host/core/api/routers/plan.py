# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import plan_to_schema
from qwenpaw_data.host.core.api.models.requests import PlanEditRequest
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.tracing.records import tracer_record_behavior
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.repositories import ChatCommandRepository, ChatRepository
from qwenpaw_data.host.core.utils.plan import to_task_context
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.utils.time import utcnow

router = APIRouter(
    prefix="/sessions/{session_id}/chats/{chat_id}/plan",
    tags=["plan"],
)


def _plan_payload(body: PlanEditRequest) -> dict[str, Any] | None:
    if body.plan is None:
        return None
    return body.plan.model_dump(mode="json")


@router.post("/edit")
async def plan_edit(
    session_id: str,
    chat_id: str,
    body: PlanEditRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        payload = _plan_payload(body)
        log_info(
            request_id=chat_id,
            step=Step.CHAT_PLAN,
            message="Chat plan edited",
            context={
                "session_id": session_id,
                "reason": body.reason,
                "plan": payload,
            },
        )
        chat_repo = ChatRepository(db, identity)
        chat = await chat_repo.get(chat_id, session_id=session_id)
        context = to_task_context(payload)
        if chat.is_active():
            await ChatCommandRepository(db).add(
                ChatCommand.plan(chat, reason=body.reason, plan=payload)
            )
        else:
            chat.plan = context
            chat.updated_at = utcnow()
            await chat_repo.save(chat)
            await tracer_record_behavior(
                db,
                chat,
                kind="plan_edit",
                target_type="plan",
                target_id=chat.id,
                payload={
                    "reason": body.reason,
                    "plan": payload,
                },
                score_name="plan_edit",
                comment=body.reason,
            )
        await db.commit()
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"plan": plan_to_schema(context)}
