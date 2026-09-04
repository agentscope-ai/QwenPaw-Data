# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.time import utcnow


@dataclass
class Session:
    id: str
    identity: Identity
    agent_id: str
    title: str
    datasource_id: str | None
    chat_count: int
    channel: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    parent_session_id: str | None = None
    forked_from_chat_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        identity: Identity,
        agent_id: str = "default",
        title: str | None = None,
        datasource_id: str | None = None,
        channel: str = "console",
    ) -> Session:
        now = utcnow()
        return cls(
            id=create_id("ses"),
            identity=identity,
            agent_id=require_safe_name(agent_id),
            title=title or "",
            datasource_id=datasource_id,
            chat_count=0,
            channel=channel,
            created_at=now,
            updated_at=now,
        )

    def rename(self, title: str | None) -> None:
        if title is None:
            return
        self.title = title
        self.updated_at = utcnow()

    def bind_datasource(self, datasource_id: str) -> None:
        if not datasource_id.strip():
            raise ValueError("datasource_id is required")
        if self.datasource_id is None:
            self.datasource_id = datasource_id
            self.updated_at = utcnow()
            return
        if self.datasource_id != datasource_id:
            raise ValueError("VALIDATION: datasource_id mismatch")

    def open_chat(
        self,
        *,
        text: str,
        datasource_id: str | None,
        has_active_chat: bool,
        artifact_comments: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Chat:
        if has_active_chat:
            raise RuntimeError("CONFLICT: session already has an active chat")
        # datasource: bind if a value is given; otherwise keep the session's.
        if datasource_id is not None and datasource_id.strip():
            self.bind_datasource(datasource_id)
        sequence = self.register_chat()
        return Chat.start(
            session_id=self.id,
            identity=self.identity,
            sequence=sequence,
            datasource_id=self.datasource_id,
            text=text,
            artifact_comments=artifact_comments,
            attachments=attachments,
        )

    def register_chat(self) -> int:
        self.chat_count += 1
        self.updated_at = utcnow()
        return self.chat_count

    def soft_delete(self, *, has_active_chat: bool) -> None:
        if self.deleted_at is not None:
            return
        if has_active_chat:
            raise RuntimeError("CONFLICT: session has active chat")
        now = utcnow()
        self.deleted_at = now
        self.updated_at = now

    def derive_status(self, *, has_active_chat: bool) -> str:
        return "running" if has_active_chat else "idle"
