# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry
from qwenpaw_data.host.core.store.protocols import (
    AttachmentStore,
    ChatEventStore,
    ChatStore,
    CronStore,
    FeedbackStore,
    PreferencesStore,
    SessionStore,
    SettlementStore,
)


@dataclass
class ServiceState:
    sessions: SessionStore
    chats: ChatStore
    events: ChatEventStore
    prefs: PreferencesStore
    cron: CronStore
    settlement: SettlementStore
    attachments: AttachmentStore
    feedback: FeedbackStore
    hosts: QwenPawDataHostRegistry
    cron_manager: Any = None
    tasks: set[asyncio.Task] = field(default_factory=set)

    def track(self, task: asyncio.Task) -> None:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


def get_state(request: Request) -> ServiceState:
    return request.app.state.service


def get_identity(request: Request) -> Identity:
    user_id = (request.headers.get("X-User-Id") or "").strip()
    if user_id:
        return Identity(user_id=user_id)
    return Identity.anonymous()


async def owned_session(state: ServiceState, identity: Identity, session_id: str):
    """Do not reveal whether another principal's session exists."""
    try:
        session = await state.sessions.get(session_id)
    except LookupError:
        raise HTTPException(404, "session_not_found") from None
    if session.identity.user_id != identity.user_id:
        raise HTTPException(404, "session_not_found")
    return session


async def require_session_owner(
    request: Request, identity: Identity = Depends(get_identity)
):
    """Apply to all legacy session paths, including streams and mutations.

    Body/form references additionally call owned_session in their handlers.
    Host supplies a principal/workspace identity; session IDs are not grants.
    """
    session_id = request.path_params.get("session_id") or request.query_params.get(
        "session_id"
    )
    if session_id:
        await owned_session(get_state(request), identity, session_id)
