# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.api.models.chat import (
    ChatErrorSchema,
    ChatSchema,
    SessionSchema,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session


def session_to_schema(
    session: Session,
    *,
    has_active_chat: bool,
) -> dict[str, Any]:
    return SessionSchema(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        status=session.derive_status(  # type: ignore[arg-type]
            has_active_chat=has_active_chat,
        ),
        datasource_id=session.datasource_id,
        chat_count=session.chat_count,
        channel=session.channel,
        parent_session_id=session.parent_session_id,
        forked_from_chat_id=session.forked_from_chat_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    ).model_dump(mode="json")


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
