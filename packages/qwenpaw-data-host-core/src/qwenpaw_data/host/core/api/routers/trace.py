# -*- coding: utf-8 -*-
"""Raw message and business-event trace for a chat."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.stream_objects import dump_stream_object
from qwenpaw_data.host.core.domain.identity import Identity

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["trace"])


@router.get("/trace")
async def trace(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
    view: str = Query("raw"),
) -> dict[str, Any]:
    _ = identity
    if view not in ("raw", "business", "both"):
        raise_api("VALIDATION", "invalid view", status=400)
    try:
        await state.chats.get(chat_id, session_id=session_id)
        events = await state.events.read_after(chat_id, -1)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    messages: list[dict[str, Any]] = []
    if view in ("raw", "both"):
        for obj in events:
            if obj.object != "message":
                continue
            payload = dump_stream_object(obj)
            payload.pop("sequence_number", None)
            payload.pop("session_id", None)
            messages.append(payload)
    biz_events: list[dict[str, Any]] = []
    if view in ("business", "both"):
        biz_events = [
            obj.biz_event.model_dump(mode="json")
            for obj in events
            if obj.object == "biz_event" and obj.biz_event.channel == "main"
        ]
    return {"messages": messages, "biz_events": biz_events}
