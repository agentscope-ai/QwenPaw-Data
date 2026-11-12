# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_bundle_to_schema
from qwenpaw_data.host.core.api.models.requests import ClarificationAnswerRequest
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.clarification import ClarificationNotFound
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    SnapshotRepository,
)
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(
    prefix="/sessions/{session_id}/chats/{chat_id}", tags=["clarification"]
)


@router.post("/clarification/answer")
async def answer_clarification(
    session_id: str,
    chat_id: str,
    body: ClarificationAnswerRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Accept ask_user_question tool result; response shape is ``{ chat }``."""
    try:
        await access_session(db, identity, session_id, write=True)
        log_info(
            request_id=chat_id,
            step=Step.CHAT_CLARIFICATION,
            message="Chat clarification answered",
            context={
                "session_id": session_id,
                "clarification_id": body.clarification_id,
                "result": body.result.model_dump(mode="json"),
            },
        )
        return await answer_clarification_core(db, identity, session_id, chat_id, body)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


async def answer_clarification_core(
    db: AsyncSession,
    identity: Identity,
    session_id: str,
    chat_id: str,
    body: ClarificationAnswerRequest,
) -> dict[str, Any]:
    """Accept ask_user_question tool result; response shape is ``{ chat }``."""
    await access_session(db, identity, session_id, write=True)
    chat_repo = ChatRepository(db, identity)
    chat = await chat_repo.get(chat_id, session_id=session_id)
    if not chat.is_active():
        raise ClarificationNotFound()
    await ChatCommandRepository(db).add(
        ChatCommand.clarification(
            chat,
            clarification_id=body.clarification_id,
            result=body.result.model_dump(mode="json"),
        )
    )
    await db.commit()
    bundle = await SnapshotRepository(db, identity).bundle_chat(chat)
    return {"chat": chat_bundle_to_schema(bundle)}
