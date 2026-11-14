# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.sse import format_sse
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.db.tables import ChatRow
from qwenpaw_data.host.core.domain.chat import TERMINAL
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.utils.status import is_terminal_response

router = APIRouter(tags=["events"])


@router.get("/sessions/{session_id}/chats/{chat_id}/events")
async def chat_events(
    request: Request,
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    after_sequence_number: int = Query(-1, ge=-1),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    after = after_sequence_number
    if last_event_id is not None and last_event_id.strip().isdigit():
        after = max(after, int(last_event_id.strip()))

    try:
        async with get_session_factory()() as db:
            await access_session(db, identity, session_id)
            await ChatRepository(db, identity).get(chat_id, session_id=session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    async def gen() -> AsyncIterator[str]:
        factory = get_session_factory()
        cursor = after
        cfg = get_settings().stream
        idle = 0.0
        terminal_since: float | None = None

        while True:
            if await request.is_disconnected():
                return

            async with factory() as sdb:
                events = ChatEventRepository(sdb, identity)
                batch = await events.read_after(chat_id, cursor)
                row = await sdb.get(ChatRow, chat_id)
                if row is None:
                    raise LookupError("chat not found")
                status = row.status
                last = row.last_sequence_number
                exhausted = (
                    not batch
                    and status in TERMINAL
                    and cursor >= last
                    and await events.terminal_delivered(chat_id, cursor)
                )

            if batch:
                idle = 0.0
                for obj in batch:
                    if await request.is_disconnected():
                        return
                    yield format_sse(obj)
                    cursor = obj.sequence_number
                    if is_terminal_response(obj):
                        return
                continue

            if exhausted:
                return

            if status in TERMINAL:
                now = asyncio.get_running_loop().time()
                if terminal_since is None:
                    terminal_since = now
                elif now - terminal_since >= cfg.heartbeat_ttl_seconds:
                    # A crashed publisher may leave a terminal row without its
                    # final event. End the incomplete stream, never fake success.
                    return

            await asyncio.sleep(cfg.sse_poll_seconds)
            idle += cfg.sse_poll_seconds
            if idle >= cfg.sse_heartbeat_seconds:
                yield ": keepalive\n\n"
                idle = 0.0

    return StreamingResponse(gen(), media_type="text/event-stream")
