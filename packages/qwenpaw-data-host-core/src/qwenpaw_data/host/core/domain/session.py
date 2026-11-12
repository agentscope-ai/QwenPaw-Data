# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.time import utcnow

ShareScope = Literal["none", "workspace", "tenant", "anyone"]


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
    share_scope: ShareScope = "none"
    enable_clarification: bool = True

    @classmethod
    def create(
        cls,
        *,
        identity: Identity,
        agent_id: str = "default",
        title: str | None = None,
        datasource_id: str | None = None,
        channel: str = "console",
        parent_session_id: str | None = None,
        enable_clarification: bool = True,
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
            parent_session_id=parent_session_id,
            share_scope="none",
            enable_clarification=enable_clarification,
        )

    def is_owner(self, identity: Identity) -> bool:
        return self.identity == identity

    def can_read(self, identity: Identity) -> bool:
        if self.is_owner(identity):
            return True
        if self.share_scope == "anyone":
            return True
        if self.share_scope == "tenant":
            return identity.tenant_id == self.identity.tenant_id
        if self.share_scope == "workspace":
            return (
                identity.tenant_id == self.identity.tenant_id
                and identity.workspace_id == self.identity.workspace_id
            )
        return False

    def can_write(self, identity: Identity) -> bool:
        return self.is_owner(identity)

    def set_share_scope(self, share_scope: ShareScope) -> None:
        if self.share_scope == share_scope:
            return
        self.share_scope = share_scope
        self.updated_at = utcnow()

    def set_enable_clarification(self, enable_clarification: bool) -> None:
        if self.enable_clarification == enable_clarification:
            return
        self.enable_clarification = enable_clarification
        self.updated_at = utcnow()

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
        dataset_names: list[str] | None = None,
        enable_clarification: bool | None = None,
    ) -> Chat:
        if has_active_chat:
            raise RuntimeError("CONFLICT: session already has an active chat")
        # datasource: bind if have value; use current session otherwise
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
            dataset_names=dataset_names,
            enable_clarification=(
                self.enable_clarification
                if enable_clarification is None
                else enable_clarification
            ),
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

    def fork(self, *, at_chat: Chat, has_active_chat: bool) -> Session:
        if has_active_chat:
            raise RuntimeError("CONFLICT: session has active chat")
        if at_chat.session_id != self.id:
            raise LookupError("chat not found")
        if at_chat.status != "completed":
            raise RuntimeError("CONFLICT: chat is not completed")
        if not at_chat.agent_state:
            raise ValueError("chat agent_state is required")
        now = utcnow()
        title = self.title.strip()
        return Session(
            id=create_id("ses"),
            identity=self.identity,
            agent_id=self.agent_id,
            title=f"{title} (fork)" if title else "fork",
            datasource_id=self.datasource_id,
            chat_count=at_chat.sequence,
            channel="console",
            created_at=now,
            updated_at=now,
            parent_session_id=self.id,
            forked_from_chat_id=at_chat.id,
            enable_clarification=self.enable_clarification,
        )
