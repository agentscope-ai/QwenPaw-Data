# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import feedback_to_schema
from qwenpaw_data.host.core.api.models.requests import FeedbackRequest
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import ChatRepository, FeedbackRepository
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["feedback"])


@router.post("/feedback")
async def feedback(
    session_id: str,
    chat_id: str,
    body: FeedbackRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    chat_repo = ChatRepository(db, identity)
    try:
        await access_session(db, identity, session_id, write=True)
        chat = await chat_repo.get(chat_id, session_id=session_id)
        feedback_repo = FeedbackRepository(db, identity)
        if body.kind in {"like", "dislike"}:
            fb = await feedback_repo.set_reaction(
                chat,
                kind=body.kind,
                reason=body.reason,
                detail=body.detail,
            )
        else:
            fb = await feedback_repo.add(
                chat,
                kind=body.kind,
                reason=body.reason,
                detail=body.detail,
                artifact_ref=(
                    body.artifact_ref.model_dump(mode="json")
                    if body.artifact_ref
                    else None
                ),
            )
        await chat_repo.save(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"feedback": feedback_to_schema(fb)}


@router.get("/feedback/reaction")
async def get_feedback_reaction(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id)
        chat = await ChatRepository(db, identity).get(chat_id, session_id=session_id)
        reaction = await FeedbackRepository(db, identity).get_reaction(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"feedback": feedback_to_schema(reaction) if reaction else None}


@router.delete("/feedback/reaction", status_code=204)
async def delete_feedback_reaction(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    chat_repo = ChatRepository(db, identity)
    try:
        await access_session(db, identity, session_id, write=True)
        chat = await chat_repo.get(chat_id, session_id=session_id)
        await FeedbackRepository(db, identity).clear_reaction(chat)
        await chat_repo.save(chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return Response(status_code=204)
