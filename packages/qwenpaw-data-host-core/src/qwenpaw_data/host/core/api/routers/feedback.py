# -*- coding: utf-8 -*-
"""Like/dislike reactions and appended feedback on a chat."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.chat import FeedbackSchema
from qwenpaw_data.host.core.api.models.requests import FeedbackRequest
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.time import utcnow

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["feedback"])


def _to_schema(data: dict[str, Any]) -> dict[str, Any]:
    return FeedbackSchema.model_validate(data).model_dump(mode="json")


async def _touch_chat(state: ServiceState, chat) -> None:
    chat.updated_at = utcnow()
    await state.chats.save(chat)


@router.post("/feedback")
async def feedback(
    session_id: str,
    chat_id: str,
    body: FeedbackRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        if body.kind in {"like", "dislike"}:
            fb = await state.feedback.set_reaction(
                user_id=identity.user_id,
                session_id=session_id,
                chat_id=chat_id,
                kind=body.kind,
                reason=body.reason,
                detail=body.detail,
            )
        else:
            fb = await state.feedback.add(
                user_id=identity.user_id,
                session_id=session_id,
                chat_id=chat_id,
                kind=body.kind,
                reason=body.reason,
                detail=body.detail,
                artifact_ref=(
                    body.artifact_ref.model_dump(mode="json")
                    if body.artifact_ref
                    else None
                ),
            )
        await _touch_chat(state, chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"feedback": _to_schema(fb)}


@router.get("/feedback/reaction")
async def get_feedback_reaction(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await state.chats.get(chat_id, session_id=session_id)
        reaction = await state.feedback.get_reaction(
            identity.user_id, session_id, chat_id
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"feedback": _to_schema(reaction) if reaction else None}


@router.delete("/feedback/reaction", status_code=204)
async def delete_feedback_reaction(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> Response:
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        await state.feedback.clear_reaction(identity.user_id, session_id, chat_id)
        await _touch_chat(state, chat)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return Response(status_code=204)
