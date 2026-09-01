# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import session_to_schema
from qwenpaw_data.host.core.api.models.requests import (
    CreateSessionRequest,
    PatchSessionRequest,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session

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
