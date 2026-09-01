# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.sse import format_sse
from qwenpaw_data.host.core.domain.chat import TERMINAL
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.stream.hub import get_hub

router = APIRouter(tags=["events"])

_HEARTBEAT_ENV = "QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS"


def _heartbeat_seconds() -> float:
    raw = (os.environ.get(_HEARTBEAT_ENV) or "").strip()
    try:
        value = float(raw) if raw else 15.0
    except ValueError:
        return 15.0
    return value if value > 0 else 15.0


@router.get("/sessions/{session_id}/chats/{chat_id}/events")
async def chat_events(
    request: Request,
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
    after_sequence_number: int = Query(-1, ge=-1),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _ = identity
    after = after_sequence_number
    if last_event_id is not None and last_event_id.strip().isdigit():
        after = max(after, int(last_event_id.strip()))

    try:
        await state.chats.get(chat_id, session_id=session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    async def gen() -> AsyncIterator[str]:
        hub = get_hub()
        cursor = after

        replay = await state.events.read_after(chat_id, cursor)
        chat = await state.chats.get(chat_id)
        status = chat.status
        last = await state.events.last_sequence_number(chat_id)

        for obj in replay:
            if await request.is_disconnected():
                return
            yield format_sse(obj)
            cursor = obj.sequence_number

        if status in TERMINAL and cursor >= last:
            return

        async for obj in hub.subscribe_live(
            chat_id,
            heartbeat_interval=_heartbeat_seconds(),
        ):
            if await request.is_disconnected():
                return
            if obj is None:
                # SSE comments keep idle-connection timers from expiring
                # without becoming visible to the client-side event parser.
                yield ": keepalive\n\n"
                continue
            if obj.sequence_number <= cursor:
                continue
            yield format_sse(obj)
            cursor = obj.sequence_number
            if obj.object == "response" and obj.status in (
                "completed",
                "failed",
                "cancelled",
            ):
                return

    return StreamingResponse(gen(), media_type="text/event-stream")
