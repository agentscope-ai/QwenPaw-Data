# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories.artifact import ArtifactRepository
from qwenpaw_data.host.core.repositories.biz_event import BizEventRepository
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.repositories.segment import SegmentRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.utils.session import access_session


@dataclass
class ChatBundle:
    chat: Chat
    segments: list[dict[str, Any]]
    biz_events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    followup: dict[str, Any] | None


class SnapshotRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def load(
        self,
        session_id: str,
        *,
        limit: int = 15,
        before_sequence: int | None = None,
    ) -> tuple[Session, bool, list[ChatBundle], bool]:
        session = await access_session(self.db, self.identity, session_id)
        active = await SessionRepository(self.db, self.identity).has_active_chat(
            session_id
        )
        chats = await ChatRepository(self.db, self.identity).list_for_session(
            session_id, before_sequence=before_sequence, limit=limit + 1
        )
        has_more = len(chats) > limit
        bundles = await self.bundle_chats(chats[-limit:] if has_more else chats)
        return session, active, bundles, has_more

    async def bundle_chats(self, chats: list[Chat]) -> list[ChatBundle]:
        chat_ids = [c.id for c in chats]
        segments = await SegmentRepository(self.db, self.identity).list_for_chats(
            chat_ids
        )
        biz_events = await BizEventRepository(self.db, self.identity).list_for_chats(
            chat_ids
        )
        artifacts = await ArtifactRepository(self.db, self.identity).list_for_chats(
            chat_ids
        )
        followups = await FollowUpRepository(self.db, self.identity).list_for_chats(
            chat_ids
        )
        return [
            ChatBundle(
                chat=c,
                segments=segments.get(c.id, []),
                biz_events=biz_events.get(c.id, []),
                artifacts=artifacts.get(c.id, []),
                followup=followups.get(c.id),
            )
            for c in chats
        ]

    async def bundle_chat(self, chat: Chat) -> ChatBundle:
        return (await self.bundle_chats([chat]))[0]
