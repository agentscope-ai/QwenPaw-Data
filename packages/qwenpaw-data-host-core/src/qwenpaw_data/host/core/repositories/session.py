# -*- coding: utf-8 -*-
from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.domain.chat import ACTIVE
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity


class SessionRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _from_row(self, row: SessionRow) -> Session:
        return Session(
            id=row.id,
            identity=Identity(
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
            ),
            agent_id=row.agent_id,
            title=row.title,
            datasource_id=row.datasource_id,
            chat_count=row.chat_count,
            channel=row.channel,
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at,
            parent_session_id=row.parent_session_id,
            forked_from_chat_id=row.forked_from_chat_id,
            share_scope=row.share_scope,
            enable_clarification=row.enable_clarification,
        )

    def _apply(self, row: SessionRow, session: Session) -> None:
        row.title = session.title
        row.datasource_id = session.datasource_id
        row.chat_count = session.chat_count
        row.channel = session.channel
        row.share_scope = session.share_scope
        row.enable_clarification = session.enable_clarification
        row.updated_at = session.updated_at
        row.deleted_at = session.deleted_at

    async def add(self, session: Session) -> None:
        self.db.add(
            SessionRow(
                id=session.id,
                user_id=session.identity.user_id,
                tenant_id=session.identity.tenant_id,
                workspace_id=session.identity.workspace_id,
                agent_id=session.agent_id,
                title=session.title,
                datasource_id=session.datasource_id,
                channel=session.channel,
                chat_count=session.chat_count,
                parent_session_id=session.parent_session_id,
                forked_from_chat_id=session.forked_from_chat_id,
                share_scope=session.share_scope,
                enable_clarification=session.enable_clarification,
                created_at=session.created_at,
                updated_at=session.updated_at,
                deleted_at=session.deleted_at,
            )
        )
        await self.db.flush()

    async def get(self, session_id: str) -> Session:
        row = await self.db.get(SessionRow, session_id)
        if row is None or row.deleted_at is not None:
            raise LookupError("session not found")
        return self._from_row(row)

    async def save(self, session: Session) -> None:
        if not session.can_write(self.identity):
            raise LookupError("session not found")
        row = await self.db.get(SessionRow, session.id)
        if row is None:
            raise LookupError("session not found")
        self._apply(row, session)
        await self.db.flush()

    async def has_active_chat(self, session_id: str) -> bool:
        n = await self.db.scalar(
            select(func.count())
            .select_from(ChatRow)
            .where(ChatRow.session_id == session_id, ChatRow.status.in_(ACTIVE))
        )
        if n is None:
            raise RuntimeError("count query returned None")
        return n > 0

    async def list(
        self,
        *,
        session_id: str | None = None,
        search_text: str | None = None,
        status: str | None = None,
        datasource_id: str | None = None,
        channel: str | None = None,
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
        min_chat_count: int = 0,
    ) -> tuple[list[tuple[Session, bool]], int]:
        if page < 1 or page_size < 1:
            raise ValueError("page/page_size invalid")
        has_active = exists().where(
            ChatRow.session_id == SessionRow.id,
            ChatRow.status.in_(ACTIVE),
        )
        q = select(SessionRow, has_active).where(
            SessionRow.user_id == self.identity.user_id,
            SessionRow.tenant_id == self.identity.tenant_id,
            SessionRow.workspace_id == self.identity.workspace_id,
            SessionRow.deleted_at.is_(None),
        )
        if session_id:
            q = q.where(SessionRow.id == session_id)
        if min_chat_count > 0:
            q = q.where(SessionRow.chat_count >= min_chat_count)
        if search_text:
            q = q.where(SessionRow.title.contains(search_text))
        if datasource_id:
            q = q.where(SessionRow.datasource_id == datasource_id)
        if channel:
            q = q.where(SessionRow.channel == channel)
        if status == "running":
            q = q.where(has_active)
        elif status == "idle":
            q = q.where(~has_active)
        if sort == "chat_count_desc":
            q = q.order_by(SessionRow.chat_count.desc(), SessionRow.updated_at.desc())
        elif sort == "updated_asc":
            q = q.order_by(SessionRow.updated_at.asc())
        else:
            q = q.order_by(SessionRow.updated_at.desc())
        # Stable ordering prevents overlaps when timestamps or counts tie.
        q = q.order_by(SessionRow.id)
        total = await self.db.scalar(
            select(func.count()).select_from(q.order_by(None).subquery())
        )
        if total is None:
            raise RuntimeError("count query returned None")
        rows = (
            await self.db.execute(q.offset((page - 1) * page_size).limit(page_size))
        ).all()
        return [(self._from_row(row), bool(active)) for row, active in rows], total

    async def delete(self, session_id: str) -> Session | None:
        row = await self.db.get(SessionRow, session_id)
        if row is None or row.deleted_at is not None:
            return None
        session = self._from_row(row)
        if not session.can_write(self.identity):
            raise LookupError("session not found")
        session.soft_delete(has_active_chat=await self.has_active_chat(session_id))
        self._apply(row, session)
        await self.db.flush()
        return session
