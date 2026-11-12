# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import SteerRequest
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.steer import SteerChatEndedError
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.repositories import ChatCommandRepository, ChatRepository
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["steer"])


@router.post("/steer")
async def steer(
    session_id: str,
    chat_id: str,
    body: SteerRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        comments = [item.model_dump(mode="json") for item in body.artifact_comments]
        log_info(
            request_id=chat_id,
            step=Step.CHAT_STEER,
            message="Chat steer requested",
            context={
                "session_id": session_id,
                "text": body.text,
                "artifact_comments": comments,
            },
        )
        chat = await ChatRepository(db, identity).get(chat_id, session_id=session_id)
        if not chat.is_active():
            raise SteerChatEndedError()
        await ChatCommandRepository(db).add(
            ChatCommand.steer(chat, body.text, comments)
        )
        await db.commit()
        return {"ok": True}
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
