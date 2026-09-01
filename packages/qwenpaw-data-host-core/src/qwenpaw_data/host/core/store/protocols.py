# -*- coding: utf-8 -*-
"""Persistence contracts for chats and stream events.

Method shapes mirror the enterprise edition's repository layer so a
database-backed implementation can be substituted without touching the
runtime.
"""

from __future__ import annotations

from typing import Any, Protocol

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session


class SessionStore(Protocol):
    async def add(self, session: Session) -> None: ...

    async def get(self, session_id: str) -> Session: ...

    async def save(self, session: Session) -> None: ...

    async def has_active_chat(self, session_id: str) -> bool: ...

    async def list(
        self,
        *,
        search_text: str | None = None,
        status: str | None = None,
        datasource_id: str | None = None,
        channel: str | None = None,
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[Session, bool]], int]: ...

    async def delete(self, session_id: str) -> None: ...


class ChatStore(Protocol):
    async def add(self, chat: Chat) -> None: ...

    async def get(
        self,
        chat_id: str,
        *,
        session_id: str | None = None,
    ) -> Chat: ...

    async def save(self, chat: Chat) -> None: ...

    async def get_active_for_session(self, session_id: str) -> Chat | None: ...

    async def list_for_session(self, session_id: str) -> list[Chat]: ...

    async def list_active(self) -> list[Chat]: ...

    async def update_plan(self, chat_id: str, plan: dict[str, Any]) -> None: ...

    async def reload_event_watermark(self, chat: Chat) -> None: ...


class ChatEventStore(Protocol):
    async def append(
        self,
        *,
        session_id: str,
        chat_id: str,
        payload: dict[str, Any],
    ) -> StreamObject: ...

    async def read_after(self, chat_id: str, after: int) -> list[StreamObject]: ...

    async def last_sequence_number(self, chat_id: str) -> int: ...
