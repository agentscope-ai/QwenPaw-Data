# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import SteerRequest
from qwenpaw_data.host.core.domain.chat import ACTIVE
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.steer import SteerChatEndedError
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry

router = APIRouter(prefix="/sessions/{session_id}/chats/{chat_id}", tags=["steer"])


@router.post("/steer", status_code=204, response_class=Response)
async def steer(
    session_id: str,
    chat_id: str,
    body: SteerRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> Response:
    _ = identity
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        if chat.status not in ACTIVE:
            raise SteerChatEndedError()
        runtime = get_runtime_registry().get(chat_id)
        if runtime is None:
            raise SteerChatEndedError()
        await runtime.steer(
            body.text,
            [item.model_dump(mode="json") for item in body.artifact_comments],
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return Response(status_code=204)
