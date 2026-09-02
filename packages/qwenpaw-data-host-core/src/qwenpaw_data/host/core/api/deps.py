# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any
from dataclasses import dataclass, field

from fastapi import Request

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry
from qwenpaw_data.host.core.store.protocols import (
    ChatEventStore,
    ChatStore,
    CronStore,
    PreferencesStore,
    SessionStore,
)


@dataclass
class ServiceState:
    sessions: SessionStore
    chats: ChatStore
    events: ChatEventStore
    prefs: PreferencesStore
    cron: CronStore
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
