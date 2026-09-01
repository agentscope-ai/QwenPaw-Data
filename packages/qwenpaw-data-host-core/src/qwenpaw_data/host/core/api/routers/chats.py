# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.chat import ChatErrorSchema, ChatSchema
from qwenpaw_data.host.core.api.models.common import ApiModel
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.stream.output_stream import OutputStream

router = APIRouter(tags=["chats"])


class CreateChatRequest(ApiModel):
    text: str
    datasource_id: str | None = None


def chat_to_schema(chat: Chat) -> dict[str, Any]:
    return ChatSchema(
        id=chat.id,
        session_id=chat.session_id,
        sequence=chat.sequence,
        user_input=chat.user_input,
        datasource_id=chat.datasource_id,
        kind=chat.kind,  # type: ignore[arg-type]
        status=chat.status,  # type: ignore[arg-type]
        last_sequence_number=chat.last_sequence_number,
        started_at=chat.started_at,
        completed_at=chat.completed_at,
        active_duration_ms=chat.active_duration_ms,
        error=ChatErrorSchema(**chat.error) if chat.error else None,
        plan=chat.plan,
    ).model_dump(mode="json")


@router.post("/sessions/{session_id}/chats")
async def create_chat(
    session_id: str,
    body: CreateChatRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        active = await state.chats.get_active_for_session(session_id)
        if active is not None:
            raise RuntimeError(
                f"CONFLICT: session already has an active chat: {active.id}",
            )
        existing = await state.chats.list_for_session(session_id)
        chat = Chat.start(
            session_id=session_id,
            identity=identity,
            sequence=len(existing) + 1,
            datasource_id=body.datasource_id,
            text=body.text,
        )
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
    )
    state.track(asyncio.create_task(runtime.run(chat.id, identity=identity)))
    return {"chat": chat_to_schema(chat)}


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
