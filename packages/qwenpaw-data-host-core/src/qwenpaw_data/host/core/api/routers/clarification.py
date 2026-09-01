# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.api.models.requests import ClarificationAnswerRequest
from qwenpaw_data.host.core.domain.clarification import ClarificationNotFound
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry

router = APIRouter(
    prefix="/sessions/{session_id}/chats/{chat_id}", tags=["clarification"]
)


@router.post("/clarification/answer")
async def answer_clarification(
    session_id: str,
    chat_id: str,
    body: ClarificationAnswerRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    """Accept an ask_user_question tool result; response shape is ``{chat}``."""
    _ = identity
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        runtime = get_runtime_registry().get(chat_id)
        if runtime is None:
            raise ClarificationNotFound()
        runtime.answer(
            clarification_id=body.clarification_id,
            result=body.result.model_dump(mode="json"),
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"chat": chat_to_schema(chat)}
