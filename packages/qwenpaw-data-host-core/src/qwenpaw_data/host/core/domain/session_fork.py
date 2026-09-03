# -*- coding: utf-8 -*-
"""Pure fork bookkeeping: id remapping and payload rewriting."""
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.domain.attachment import (
    Attachment,
    uploads_relative_path,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.utils.ids import create_id


class SessionFork:
    def __init__(
        self,
        source: Session,
        at_chat: Chat,
        *,
        has_active_chat: bool,
    ) -> None:
        self.source = source
        self.at_chat = at_chat
        self.target = source.fork(at_chat=at_chat, has_active_chat=has_active_chat)
        self._ids = {source.id: self.target.id}

    def remap(self, old_id: str, prefix: str) -> str:
        if old_id not in self._ids:
            self._ids[old_id] = create_id(prefix)
        return self._ids[old_id]

    def mapped(self, old_id: str) -> str:
        return self._ids[old_id]

    def rewrite(self, value: Any) -> Any:
        if isinstance(value, str):
            for old, new in self._ids.items():
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [self.rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: self.rewrite(item) for key, item in value.items()}
        return value

    def copy_chat(self, chat: Chat) -> Chat:
        return Chat(
            id=self.mapped(chat.id),
            session_id=self.target.id,
            identity=self.target.identity,
            sequence=chat.sequence,
            user_input=self.rewrite(chat.user_input),
            datasource_id=chat.datasource_id,
            kind=chat.kind,
            status=chat.status,
            last_sequence_number=chat.last_sequence_number,
            started_at=chat.started_at,
            completed_at=chat.completed_at,
            active_duration_ms=chat.active_duration_ms,
            error=self.rewrite(chat.error),
            plan=self.rewrite(chat.plan),
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            artifact_comments=self.rewrite(chat.artifact_comments),
            attachments=self.rewrite(chat.attachments),
        )

    def copy_attachment(self, attachment: Attachment) -> Attachment:
        return Attachment(
            id=self.mapped(attachment.id),
            session_id=self.target.id,
            identity=self.target.identity,
            filename=attachment.filename,
            storage_path=uploads_relative_path(
                self.target.id, attachment.filename
            ),
            created_at=attachment.created_at,
        )
