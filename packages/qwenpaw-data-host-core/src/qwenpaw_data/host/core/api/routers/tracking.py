# -*- coding: utf-8 -*-
"""Browser signals that never hit the server on their own."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.common import ApiModel
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import tracer_record_console
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/console", tags=["console"])

ConsoleEventKind = Literal["trace_view", "segment_toggle", "session_revisit"]


class ConsoleEventRequest(ApiModel):
    kind: ConsoleEventKind
    session_id: str
    chat_id: str | None = None
    target_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/events")
async def post_console_event(
    body: ConsoleEventRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    try:
        await access_session(db, identity, body.session_id)
        await tracer_record_console(
            db,
            identity,
            session_id=body.session_id,
            chat_id=body.chat_id,
            kind=body.kind,
            target_id=body.target_id,
            payload=body.payload,
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True}
