# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_to_schema, session_to_schema
from qwenpaw_data.host.core.api.models.requests import (
    CreateSessionRequest,
    ForkSessionRequest,
    PatchSessionRequest,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.fork import fork_session
from qwenpaw_data.host.core.store.protocols import ChatEventStore

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(
    body: CreateSessionRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        session = Session.create(
            identity=identity,
            agent_id=body.agent_id,
            title=body.title,
            datasource_id=body.datasource_id,
        )
        await state.sessions.add(session)
        return {"session": session_to_schema(session, has_active_chat=False)}
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.get("")
async def list_sessions(
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
    search_text: str | None = None,
    status: str | None = None,
    datasource_id: str | None = None,
    channel: str | None = None,
    sort: str = "updated_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    _ = identity
    try:
        items, total = await state.sessions.list(
            search_text=search_text,
            status=status,
            datasource_id=datasource_id,
            channel=channel,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "items": [
            session_to_schema(s, has_active_chat=a) for s, a in items
        ],
        "total": total,
    }


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        session = await state.sessions.get(session_id)
        return {
            "session": session_to_schema(
                session,
                has_active_chat=await state.sessions.has_active_chat(session.id),
            )
        }
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.patch("/{session_id}")
async def patch_session(
    session_id: str,
    body: PatchSessionRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        session = await state.sessions.get(session_id)
        session.rename(body.title)
        await state.sessions.save(session)
        return {
            "session": session_to_schema(
                session,
                has_active_chat=await state.sessions.has_active_chat(session.id),
            )
        }
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> None:
    _ = identity
    try:
        await state.sessions.delete(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.post("/{session_id}/fork")
async def fork(
    session_id: str,
    body: ForkSessionRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        session = await fork_session(
            sessions=state.sessions,
            chats=state.chats,
            events=state.events,
            attachments=state.attachments,
            home=state.hosts.home,
            session_id=session_id,
            chat_id=body.chat_id,
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"session": session_to_schema(session, has_active_chat=False)}


async def bundle_extras(
    events: ChatEventStore,
    chat_id: str,
) -> dict[str, Any]:
    """Segments, main-channel biz events, artifacts, last followup."""
    segments: list[dict[str, Any]] = []
    biz_events: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    followup: dict[str, Any] | None = None
    for obj in await events.read_after(chat_id, -1):
        if obj.object == "segment":
            segments.append(obj.segment.model_dump(mode="json"))
        elif obj.object == "biz_event" and obj.biz_event.channel == "main":
            biz_events.append(obj.biz_event.model_dump(mode="json"))
        elif obj.object == "artifact.registered":
            artifacts.append(obj.artifact.model_dump(mode="json"))
        elif obj.object == "followup.generated":
            followup = obj.followup.model_dump(mode="json")
    return {
        "segments": segments,
        "biz_events": biz_events,
        "artifacts": artifacts,
        "followup": followup,
    }


@router.get("/{session_id}/snapshot")
async def snapshot(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        session = await state.sessions.get(session_id)
        active = await state.sessions.has_active_chat(session_id)
        chats = await state.chats.list_for_session(session_id)
        bundles = [
            chat_to_schema(chat, await bundle_extras(state.events, chat.id))
            for chat in chats
        ]
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "session": session_to_schema(session, has_active_chat=active),
        "chats": bundles,
    }
