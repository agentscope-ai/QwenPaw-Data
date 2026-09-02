# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.api.models.requests import CreateChatRequest
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.stream.output_stream import OutputStream

router = APIRouter(tags=["chats"])


@router.post("/sessions/{session_id}/chats")
async def create_chat(
    session_id: str,
    body: CreateChatRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        session = await state.sessions.get(session_id)
        chat = session.open_chat(
            text=body.text,
            datasource_id=body.datasource_id,
            has_active_chat=await state.sessions.has_active_chat(session_id),
        )
        await state.sessions.save(session)
        await state.chats.add(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    runtime = ChatRuntime(
        chats=state.chats,
        events=state.events,
        hosts=state.hosts,
        prefs=state.prefs,
        sessions=state.sessions,
        settlement=state.settlement,
    )
    state.track(asyncio.create_task(runtime.run(chat.id, identity=identity)))
    return {"chat": chat_to_schema(chat)}


@router.get("/sessions/{session_id}/chats")
async def list_chats(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        await state.sessions.get(session_id)
        chats = await state.chats.list_for_session(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"items": [chat_to_schema(c) for c in chats]}


@router.post("/sessions/{session_id}/chats/{chat_id}/stop")
async def stop_chat(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        runtime = get_runtime_registry().get(chat_id)
        if runtime is not None:
            await runtime.cancel()
            chat = await state.chats.get(chat_id, session_id=session_id)
        elif chat.status == "running":
            # Orphaned running chat (e.g. after a crash): emit the terminal
            # event and persist the cancellation directly.
            await OutputStream(
                state.events,
                session_id=session_id,
                chat_id=chat_id,
                identity=chat.identity,
            ).response_cancelled()
            chat.cancel()
            await state.chats.reload_event_watermark(chat)
            await state.chats.save(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"chat": chat_to_schema(chat)}
