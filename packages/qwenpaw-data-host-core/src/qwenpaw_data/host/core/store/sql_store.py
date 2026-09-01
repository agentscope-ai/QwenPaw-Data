# -*- coding: utf-8 -*-
"""SQLAlchemy implementations of the store protocols.

Unlike the enterprise repositories (per-request AsyncSession injected by
the API layer), these stores are long-lived objects holding a session
factory: every method opens its own session and commits, matching the
JSON stores' self-contained persistence semantics that ChatRuntime and
the routers rely on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    dump_stream_object,
    parse_stream_object,
)
from qwenpaw_data.host.core.db.tables import ChatEventRow, ChatRow, SessionRow
from qwenpaw_data.host.core.domain.chat import ACTIVE, Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.utils.time import utcnow


def _session_from_row(row: SessionRow) -> Session:
    return Session(
        id=row.id,
        identity=Identity(user_id=row.user_id),
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
    )


def _apply_session(row: SessionRow, session: Session) -> None:
    row.title = session.title
    row.datasource_id = session.datasource_id
    row.chat_count = session.chat_count
    row.channel = session.channel
    row.updated_at = session.updated_at
    row.deleted_at = session.deleted_at


class SQLSessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, session: Session) -> None:
        async with self._sessions() as db:
            existing = await db.get(SessionRow, session.id)
            if existing is not None:
                raise RuntimeError(
                    f"CONFLICT: session already exists: {session.id}"
                )
            db.add(
                SessionRow(
                    id=session.id,
                    user_id=session.identity.user_id,
                    agent_id=session.agent_id,
                    title=session.title,
                    datasource_id=session.datasource_id,
                    channel=session.channel,
                    chat_count=session.chat_count,
                    parent_session_id=session.parent_session_id,
                    forked_from_chat_id=session.forked_from_chat_id,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    deleted_at=session.deleted_at,
                )
            )
            await db.commit()

    async def get(self, session_id: str) -> Session:
        async with self._sessions() as db:
            row = await db.get(SessionRow, session_id)
            if row is None or row.deleted_at is not None:
                raise LookupError(f"session not found: {session_id}")
            return _session_from_row(row)

    async def save(self, session: Session) -> None:
        async with self._sessions() as db:
            row = await db.get(SessionRow, session.id)
            if row is None:
                raise LookupError(f"session not found: {session.id}")
            _apply_session(row, session)
            await db.commit()

    async def has_active_chat(self, session_id: str) -> bool:
        async with self._sessions() as db:
            n = await db.scalar(
                select(func.count())
                .select_from(ChatRow)
                .where(
                    ChatRow.session_id == session_id,
                    ChatRow.status.in_(ACTIVE),
                )
            )
            return bool(n)

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
    ) -> tuple[list[tuple[Session, bool]], int]:
        if page < 1 or page_size < 1:
            raise ValueError("page/page_size invalid")
        async with self._sessions() as db:
            has_active = exists().where(
                ChatRow.session_id == SessionRow.id,
                ChatRow.status.in_(ACTIVE),
            )
            q = select(SessionRow, has_active).where(
                SessionRow.deleted_at.is_(None),
            )
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
                q = q.order_by(
                    SessionRow.chat_count.desc(), SessionRow.updated_at.desc()
                )
            elif sort == "updated_asc":
                q = q.order_by(SessionRow.updated_at.asc())
            else:
                q = q.order_by(SessionRow.updated_at.desc())
            total = await db.scalar(
                select(func.count()).select_from(q.order_by(None).subquery())
            )
            rows = (
                await db.execute(q.offset((page - 1) * page_size).limit(page_size))
            ).all()
            return (
                [(_session_from_row(row), bool(active)) for row, active in rows],
                int(total or 0),
            )

    async def delete(self, session_id: str) -> None:
        active = await self.has_active_chat(session_id)
        async with self._sessions() as db:
            row = await db.get(SessionRow, session_id)
            if row is None or row.deleted_at is not None:
                return
            session = _session_from_row(row)
            session.soft_delete(has_active_chat=active)
            _apply_session(row, session)
            await db.commit()


def _chat_from_row(row: ChatRow) -> Chat:
    return Chat(
        id=row.id,
        session_id=row.session_id,
        identity=Identity(user_id=row.user_id),
        sequence=row.sequence,
        user_input=row.user_input,
        datasource_id=row.datasource_id,
        kind=row.kind,
        status=row.status,
        last_sequence_number=row.last_sequence_number,
        started_at=row.started_at,
        completed_at=row.completed_at,
        active_duration_ms=row.active_duration_ms,
        error=row.error_json,
        plan=row.plan,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply_chat(row: ChatRow, chat: Chat) -> None:
    row.user_input = chat.user_input
    row.datasource_id = chat.datasource_id
    row.kind = chat.kind
    row.status = chat.status
    row.started_at = chat.started_at
    row.completed_at = chat.completed_at
    row.active_duration_ms = chat.active_duration_ms
    row.error_json = chat.error
    row.plan = chat.plan
    row.updated_at = chat.updated_at


class SQLChatStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, chat: Chat) -> None:
        async with self._sessions() as db:
            existing = await db.get(ChatRow, chat.id)
            if existing is not None:
                raise RuntimeError(f"CONFLICT: chat already exists: {chat.id}")
            db.add(
                ChatRow(
                    id=chat.id,
                    session_id=chat.session_id,
                    user_id=chat.identity.user_id,
                    sequence=chat.sequence,
                    user_input=chat.user_input,
                    datasource_id=chat.datasource_id,
                    kind=chat.kind,
                    status=chat.status,
                    last_sequence_number=chat.last_sequence_number,
                    started_at=chat.started_at,
                    completed_at=chat.completed_at,
                    active_duration_ms=chat.active_duration_ms,
                    error_json=chat.error,
                    plan=chat.plan,
                    created_at=chat.created_at,
                    updated_at=chat.updated_at,
                )
            )
            await db.commit()

    async def get(
        self,
        chat_id: str,
        *,
        session_id: str | None = None,
    ) -> Chat:
        async with self._sessions() as db:
            row = await db.get(ChatRow, chat_id)
            if row is None:
                raise LookupError(f"chat not found: {chat_id}")
            if session_id is not None and row.session_id != session_id:
                raise LookupError(f"chat not found: {chat_id}")
            return _chat_from_row(row)

    async def save(self, chat: Chat) -> None:
        async with self._sessions() as db:
            row = await db.get(ChatRow, chat.id)
            if row is None:
                raise LookupError(f"chat not found: {chat.id}")
            _apply_chat(row, chat)
            await db.commit()

    async def get_active_for_session(self, session_id: str) -> Chat | None:
        async with self._sessions() as db:
            row = (
                await db.scalars(
                    select(ChatRow)
                    .where(
                        ChatRow.session_id == session_id,
                        ChatRow.status.in_(ACTIVE),
                    )
                    .order_by(ChatRow.sequence.desc())
                )
            ).first()
            return _chat_from_row(row) if row is not None else None

    async def list_for_session(self, session_id: str) -> list[Chat]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ChatRow)
                    .where(ChatRow.session_id == session_id)
                    .order_by(ChatRow.sequence)
                )
            ).all()
            return [_chat_from_row(r) for r in rows]

    async def list_active(self) -> list[Chat]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ChatRow).where(ChatRow.status.in_(ACTIVE))
                )
            ).all()
            return [_chat_from_row(r) for r in rows]

    async def update_plan(self, chat_id: str, plan: dict[str, Any]) -> None:
        async with self._sessions() as db:
            row = await db.get(ChatRow, chat_id)
            if row is None:
                raise LookupError(f"chat not found: {chat_id}")
            row.plan = plan
            row.updated_at = utcnow()
            await db.commit()

    async def reload_event_watermark(self, chat: Chat) -> None:
        async with self._sessions() as db:
            row = await db.get(ChatRow, chat.id)
            if row is None:
                raise LookupError(f"chat not found: {chat.id}")
            chat.apply_event_watermark(
                last_sequence_number=row.last_sequence_number,
                updated_at=row.updated_at,
            )


class SQLChatEventStore:
    """Chat-scoped stream events; the sequence watermark lives on ChatRow."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    async def append(
        self,
        *,
        session_id: str,
        chat_id: str,
        payload: dict[str, Any],
    ) -> StreamObject:
        async with self._lock_for(chat_id):
            async with self._sessions() as db:
                chat = await db.get(ChatRow, chat_id)
                if chat is None:
                    raise ValueError(f"chat not found: {chat_id}")
                if chat.session_id != session_id:
                    raise ValueError("session_id mismatch")

                chat.last_sequence_number += 1
                chat.updated_at = utcnow()
                seq = chat.last_sequence_number
                data = {
                    **payload,
                    "sequence_number": seq,
                    "session_id": session_id,
                    "chat_id": chat_id,
                }
                obj = parse_stream_object(data)
                db.add(
                    ChatEventRow(
                        chat_id=chat_id,
                        sequence_number=seq,
                        object=obj.object,
                        session_id=session_id,
                        user_id=chat.user_id,
                        payload=dump_stream_object(obj),
                    )
                )
                await db.commit()
                return obj

    async def read_after(self, chat_id: str, after: int) -> list[StreamObject]:
        async with self._sessions() as db:
            result = await db.scalars(
                select(ChatEventRow)
                .where(
                    ChatEventRow.chat_id == chat_id,
                    ChatEventRow.sequence_number > after,
                )
                .order_by(ChatEventRow.sequence_number)
            )
            return [parse_stream_object(r.payload) for r in result.all()]

    async def last_sequence_number(self, chat_id: str) -> int:
        async with self._sessions() as db:
            chat = await db.get(ChatRow, chat_id)
            if chat is None:
                return -1
            return chat.last_sequence_number
