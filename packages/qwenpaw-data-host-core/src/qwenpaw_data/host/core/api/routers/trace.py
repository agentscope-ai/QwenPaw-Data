# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.mappers import biz_event_to_schema, message_to_schema
from qwenpaw_data.host.core.repositories import BizEventRepository, ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["trace"])


@router.get("/trace")
async def trace(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    view: str = Query("raw"),
) -> dict[str, Any]:
    if view not in ("raw", "business", "both"):
        raise_api("VALIDATION", "invalid view", status=400)
    chat_repo = ChatRepository(db, identity)
    try:
        await access_session(db, identity, session_id)
        await chat_repo.get(chat_id, session_id=session_id)
        messages = (
            await chat_repo.read_messages(chat_id) if view in ("raw", "both") else []
        )
        mcp_connections = (
            await ChatEventRepository(db, identity).read_mcp_connections(chat_id)
            if view in ("raw", "both")
            else []
        )
        timing_events = (
            await ChatEventRepository(db, identity).read_timing_events(chat_id)
            if view in ("raw", "both")
            else []
        )
        biz_events = (
            await BizEventRepository(db, identity).list_for_chat(
                chat_id, main_only=True
            )
            if view in ("business", "both")
            else []
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "timing_events": timing_events,
        "mcp_connections": [
            event.model_dump(mode="json")
            for event in mcp_connections
            if event.status == "failed"
        ],
        "messages": [message_to_schema(m) for m in messages],
        "biz_events": [biz_event_to_schema(event) for event in biz_events],
    }
