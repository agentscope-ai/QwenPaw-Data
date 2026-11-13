# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_bundle_to_schema
from qwenpaw_data.host.core.api.models.requests import ToolConfirmationAnswerRequest
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    SnapshotRepository,
)
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(
    prefix="/sessions/{session_id}/chats/{chat_id}", tags=["tool-confirmation"]
)


@router.post("/tool-confirmation/answer")
async def answer_tool_confirmation(
    session_id: str,
    chat_id: str,
    body: ToolConfirmationAnswerRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        chat = await ChatRepository(db, identity).get(chat_id, session_id=session_id)
        if not chat.is_active():
            raise RuntimeError("CONFLICT: chat is not active")
        await ChatCommandRepository(db).add(
            ChatCommand.for_tool_confirmation(
                chat, call_id=body.call_id, decision=body.decision
            )
        )
        await db.commit()
        bundle = await SnapshotRepository(db, identity).bundle_chat(chat)
        return {"chat": chat_bundle_to_schema(bundle)}
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
