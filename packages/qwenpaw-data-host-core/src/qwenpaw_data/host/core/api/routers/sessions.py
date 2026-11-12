# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import (
    chat_bundle_to_schema,
    session_to_schema,
)
from qwenpaw_data.host.core.api.models.requests import (
    CreateSessionRequest,
    ForkSessionRequest,
    PatchSessionRequest,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.tracing.records import tracer_record_behavior
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import (
    ChatRepository,
    SessionForkRepository,
    SessionRepository,
    SnapshotRepository,
)
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.workspace import get_workspace_manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(
    body: CreateSessionRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session_repo = SessionRepository(db, identity)
    try:
        if body.parent_session_id:
            await access_session(db, identity, body.parent_session_id, write=True)
        session = Session.create(
            identity=identity,
            agent_id=body.agent_id,
            title=body.title,
            datasource_id=body.datasource_id,
            parent_session_id=body.parent_session_id,
            enable_clarification=body.enable_clarification,
        )
        await session_repo.add(session)
        return {
            "session": session_to_schema(
                session, has_active_chat=await session_repo.has_active_chat(session.id)
            )
        }
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.get("")
async def list_sessions(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    session_id: str | None = None,
    search_text: str | None = None,
    status: str | None = None,
    datasource_id: str | None = None,
    channel: str | None = None,
    sort: str = "updated_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    min_chat_count: int = Query(0, ge=0),
) -> dict[str, Any]:
    session_repo = SessionRepository(db, identity)
    try:
        items, total = await session_repo.list(
            session_id=session_id,
            search_text=search_text,
            status=status,
            datasource_id=datasource_id,
            channel=channel,
            sort=sort,
            page=page,
            page_size=page_size,
            min_chat_count=min_chat_count,
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "items": [session_to_schema(s, has_active_chat=a) for s, a in items],
        "total": total,
    }


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session_repo = SessionRepository(db, identity)
    try:
        session = await access_session(db, identity, session_id)
        return {
            "session": session_to_schema(
                session, has_active_chat=await session_repo.has_active_chat(session.id)
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
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session_repo = SessionRepository(db, identity)
    try:
        session = await access_session(db, identity, session_id, write=True)
        session.rename(body.title)
        if body.share_scope is not None:
            session.set_share_scope(body.share_scope)
            await tracer_record_behavior(
                db,
                identity,
                session_id=session.id,
                kind="share",
                target_type="session",
                target_id=session.id,
                payload={"share_scope": session.share_scope},
                score_name="share",
                value=session.share_scope,
                score_on="session",
            )
        if body.enable_clarification is not None:
            session.set_enable_clarification(body.enable_clarification)
            chat_repo = ChatRepository(db, identity)
            active = await chat_repo.get_active(session.id)
            if active is not None:
                active.set_enable_clarification(body.enable_clarification)
                await chat_repo.save(active)
        await session_repo.save(session)
        return {
            "session": session_to_schema(
                session, has_active_chat=await session_repo.has_active_chat(session.id)
            )
        }
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    session_repo = SessionRepository(db, identity)
    try:
        await access_session(db, identity, session_id, write=True)
        deleted = await session_repo.delete(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    if deleted is not None:
        await get_workspace_manager().purge_session(
            UserAgentPaths.from_identity(
                home=get_settings().paths.home,
                identity=identity,
                agent_id=deleted.agent_id,
                session_id=session_id,
            ),
            session_id=session_id,
        )
    return {"status": "ok"}


@router.post("/{session_id}/fork")
async def fork_session(
    session_id: str,
    body: ForkSessionRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        session = await SessionForkRepository(db, identity).execute(
            session_id, body.chat_id
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"session": session_to_schema(session, has_active_chat=False)}


@router.get("/{session_id}/snapshot")
async def snapshot(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(15, ge=1, le=100),
    before_sequence: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    try:
        session, active, bundles, has_more = await SnapshotRepository(
            db, identity
        ).load(session_id, limit=limit, before_sequence=before_sequence)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "session": session_to_schema(session, has_active_chat=active),
        "chats": [chat_bundle_to_schema(b) for b in bundles],
        "has_more": has_more,
    }
