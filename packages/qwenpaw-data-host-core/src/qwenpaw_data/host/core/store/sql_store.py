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
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    dump_stream_object,
    parse_stream_object,
)
from qwenpaw_data.host.core.api.models.cron import CronJobWrite, ScheduleSpec
from qwenpaw_data.host.core.db.tables import (
    AttachmentRow,
    ChannelBindingRow,
    ChannelConfigRow,
    ChatEventRow,
    ChatRow,
    CronJobRow,
    SessionRow,
    SettlementCardRow,
    UserActiveModelRow,
    UserProviderModelRow,
    UserProviderRow,
)
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import ACTIVE, Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import (
    ModelOverride,
    ProviderCredential,
    UserPreferences,
)
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.store._prefs_logic import (
    clean_model_upsert,
    merge_provider_patch,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.secrets import decrypt_api_key
from qwenpaw_data.host.core.utils.time import utcnow


class SQLPreferencesStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load(self, user_id: str) -> UserPreferences:
        async with self._sessions() as db:
            providers = {
                row.provider_id: ProviderCredential(
                    api_key=decrypt_api_key(row.api_key_enc),
                    base_url=row.base_url,
                )
                for row in (
                    await db.scalars(
                        select(UserProviderRow).where(
                            UserProviderRow.user_id == user_id
                        )
                    )
                )
            }
            models = {
                (row.provider_id, row.model_id): ModelOverride(
                    source=row.source,
                    name=row.name,
                    thinking_enabled=row.thinking_enabled,
                    generate_kwargs=row.generate_kwargs_json,
                )
                for row in (
                    await db.scalars(
                        select(UserProviderModelRow).where(
                            UserProviderModelRow.user_id == user_id
                        )
                    )
                )
            }
            active = await db.get(UserActiveModelRow, user_id)
            return UserPreferences(
                user_id=user_id,
                providers=providers,
                models=models,
                default_provider_id=(
                    None if active is None else active.default_provider_id
                ),
                default_model_id=(
                    None if active is None else active.default_model_id
                ),
                light_provider_id=(
                    None if active is None else active.light_provider_id
                ),
                light_model_id=(
                    None if active is None else active.light_model_id
                ),
            )

    async def upsert_provider(
        self,
        user_id: str,
        provider_id: str,
        patch: dict[str, Any],
    ) -> None:
        async with self._sessions() as db:
            row = await db.get(UserProviderRow, (user_id, provider_id))
            api_key_enc, base_url = merge_provider_patch(
                exists=row is not None,
                current_api_key_enc=None if row is None else row.api_key_enc,
                current_base_url=None if row is None else row.base_url,
                patch=patch,
                provider_id=provider_id,
            )
            now = utcnow()
            if row is None:
                db.add(
                    UserProviderRow(
                        user_id=user_id,
                        provider_id=provider_id,
                        api_key_enc=api_key_enc,
                        base_url=base_url,
                        updated_at=now,
                    )
                )
            else:
                row.api_key_enc = api_key_enc
                row.base_url = base_url
                row.updated_at = now
            await db.commit()

    async def delete_provider(self, user_id: str, provider_id: str) -> None:
        async with self._sessions() as db:
            row = await db.get(UserProviderRow, (user_id, provider_id))
            if row is None:
                raise LookupError(f"provider config not found: {provider_id}")
            await db.delete(row)
            await db.commit()

    async def upsert_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
        *,
        source: str,
        name: str | None = None,
        thinking_enabled: bool | None = None,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        model_id, name = clean_model_upsert(
            provider_id, model_id, source=source, name=name
        )
        async with self._sessions() as db:
            row = await db.get(
                UserProviderModelRow, (user_id, provider_id, model_id)
            )
            now = utcnow()
            if row is None:
                db.add(
                    UserProviderModelRow(
                        user_id=user_id,
                        provider_id=provider_id,
                        model_id=model_id,
                        source=source,
                        name=name,
                        thinking_enabled=thinking_enabled,
                        generate_kwargs_json=generate_kwargs,
                        updated_at=now,
                    )
                )
            else:
                row.source = source
                row.name = name
                row.thinking_enabled = thinking_enabled
                row.generate_kwargs_json = generate_kwargs
                row.updated_at = now
            await db.commit()

    async def delete_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
    ) -> None:
        async with self._sessions() as db:
            row = await db.get(
                UserProviderModelRow, (user_id, provider_id, model_id)
            )
            if row is None:
                raise LookupError(
                    f"model config not found: {provider_id}/{model_id}"
                )
            await db.delete(row)
            await db.commit()

    async def get_active_models(self, user_id: str) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await db.get(UserActiveModelRow, user_id)
            if row is None:
                return {
                    "default_provider_id": None,
                    "default_model_id": None,
                    "light_provider_id": None,
                    "light_model_id": None,
                }
            return {
                "default_provider_id": row.default_provider_id,
                "default_model_id": row.default_model_id,
                "light_provider_id": row.light_provider_id,
                "light_model_id": row.light_model_id,
            }

    async def set_active_models(
        self,
        user_id: str,
        *,
        default_provider_id: str,
        default_model_id: str,
        light_provider_id: str | None = None,
        light_model_id: str | None = None,
    ) -> dict[str, Any]:
        if (light_provider_id is None) != (light_model_id is None):
            raise ValueError(
                "light_provider_id and light_model_id must be set together"
            )
        prefs = await self.load(user_id)
        prefs.default_provider_id = default_provider_id
        prefs.default_model_id = default_model_id
        prefs.light_provider_id = light_provider_id
        prefs.light_model_id = light_model_id
        prefs.validate_selection()
        async with self._sessions() as db:
            row = await db.get(UserActiveModelRow, user_id)
            now = utcnow()
            if row is None:
                db.add(
                    UserActiveModelRow(
                        user_id=user_id,
                        default_provider_id=default_provider_id,
                        default_model_id=default_model_id,
                        light_provider_id=light_provider_id,
                        light_model_id=light_model_id,
                        updated_at=now,
                    )
                )
            else:
                row.default_provider_id = default_provider_id
                row.default_model_id = default_model_id
                row.light_provider_id = light_provider_id
                row.light_model_id = light_model_id
                row.updated_at = now
            await db.commit()
        return await self.get_active_models(user_id)


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
        artifact_comments=list(row.artifact_comments_json or []),
        attachments=list(row.attachments_json or []),
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
    row.artifact_comments_json = list(chat.artifact_comments)
    row.attachments_json = list(chat.attachments)
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
                    artifact_comments_json=list(chat.artifact_comments),
                    attachments_json=list(chat.attachments),
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


def _cron_to_dict(row: CronJobRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "enabled": row.enabled,
        "message": row.message,
        "datasource_id": row.datasource_id,
        "channel": row.channel,
        "target_external_key": row.target_external_key,
        "session_id": row.session_id,
        "schedule": ScheduleSpec.model_validate(row.schedule_json).model_dump(
            mode="json"
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class SQLCronStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @staticmethod
    async def _get_row(db: AsyncSession, user_id: str, job_id: str) -> CronJobRow:
        row = await db.get(CronJobRow, job_id)
        if row is None or row.user_id != user_id:
            raise LookupError("cron job not found")
        return row

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(CronJobRow)
                    .where(CronJobRow.user_id == user_id)
                    .order_by(CronJobRow.created_at.desc())
                )
            ).all()
            return [_cron_to_dict(r) for r in rows]

    async def get(self, user_id: str, job_id: str) -> dict[str, Any]:
        async with self._sessions() as db:
            return _cron_to_dict(await self._get_row(db, user_id, job_id))

    async def create(self, user_id: str, body: CronJobWrite) -> dict[str, Any]:
        now = utcnow()
        async with self._sessions() as db:
            row = CronJobRow(
                id=create_id("cron"),
                user_id=user_id,
                name=body.name,
                enabled=body.enabled,
                message=body.message,
                datasource_id=body.datasource_id,
                channel=body.channel,
                target_external_key=body.target_external_key,
                session_id=body.session_id,
                schedule_json=body.schedule.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            await db.commit()
            return _cron_to_dict(row)

    async def replace(
        self, user_id: str, job_id: str, body: CronJobWrite
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, job_id)
            row.name = body.name
            row.enabled = body.enabled
            row.message = body.message
            row.datasource_id = body.datasource_id
            row.channel = body.channel
            row.target_external_key = body.target_external_key
            row.session_id = body.session_id
            row.schedule_json = body.schedule.model_dump(mode="json")
            row.updated_at = utcnow()
            await db.commit()
            return _cron_to_dict(row)

    async def set_enabled(
        self, user_id: str, job_id: str, enabled: bool
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, job_id)
            row.enabled = enabled
            row.updated_at = utcnow()
            await db.commit()
            return _cron_to_dict(row)

    async def delete(self, user_id: str, job_id: str) -> None:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, job_id)
            await db.delete(row)
            await db.commit()

    async def get_by_id(self, job_id: str) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await db.get(CronJobRow, job_id)
            if row is None:
                raise LookupError("cron job not found")
            return _cron_to_dict(row)

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(CronJobRow).order_by(CronJobRow.created_at.asc())
                )
            ).all()
            return [_cron_to_dict(r) for r in rows]


_SETTLEMENT_ACTIONABLE = frozenset({"pending", "queried"})


def _settlement_to_dict(row: SettlementCardRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "source_chat_id": row.source_chat_id,
        "type": row.type,
        "fields": dict(row.fields_json or {}),
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "confirmed_at": row.confirmed_at,
    }


class SQLSettlementStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @staticmethod
    async def _get_row(
        db: AsyncSession, user_id: str, card_id: str, *, session_id: str
    ) -> SettlementCardRow:
        row = await db.get(SettlementCardRow, card_id)
        if (
            row is None
            or row.session_id != session_id
            or row.user_id != user_id
        ):
            raise LookupError(f"settlement card not found: {card_id}")
        return row

    async def add(
        self,
        *,
        user_id: str,
        session_id: str,
        source_chat_id: str,
        type: str,
        fields: dict[str, str],
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._sessions() as db:
            row = SettlementCardRow(
                id=create_id("card"),
                user_id=user_id,
                session_id=session_id,
                source_chat_id=source_chat_id,
                type=type,
                fields_json=dict(fields),
                status="pending",
                created_at=now,
                updated_at=now,
                confirmed_at=None,
            )
            db.add(row)
            await db.commit()
            return _settlement_to_dict(row)

    async def list_by_session(
        self,
        user_id: str,
        session_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._sessions() as db:
            stmt = select(SettlementCardRow).where(
                SettlementCardRow.session_id == session_id,
                SettlementCardRow.user_id == user_id,
            )
            if status is not None:
                stmt = stmt.where(SettlementCardRow.status == status)
            stmt = stmt.order_by(
                SettlementCardRow.created_at.desc(),
                SettlementCardRow.id.desc(),
            )
            rows = (await db.scalars(stmt)).all()
            return [_settlement_to_dict(row) for row in rows]

    async def get(
        self, user_id: str, card_id: str, *, session_id: str
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, card_id, session_id=session_id)
            return _settlement_to_dict(row)

    async def mark_queried(
        self, user_id: str, session_id: str, card_ids: list[str]
    ) -> None:
        if not card_ids:
            return
        now = utcnow()
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(SettlementCardRow).where(
                        SettlementCardRow.id.in_(card_ids),
                        SettlementCardRow.session_id == session_id,
                        SettlementCardRow.status == "pending",
                        SettlementCardRow.user_id == user_id,
                    )
                )
            ).all()
            for row in rows:
                row.status = "queried"
                row.updated_at = now
            await db.commit()

    async def confirm(
        self,
        user_id: str,
        card_id: str,
        *,
        session_id: str,
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, card_id, session_id=session_id)
            if row.status not in _SETTLEMENT_ACTIONABLE:
                raise ValueError(f"settlement card is not actionable: {card_id}")
            now = utcnow()
            if fields is not None:
                row.fields_json = dict(fields)
            row.status = "confirmed"
            row.confirmed_at = now
            row.updated_at = now
            await db.commit()
            return _settlement_to_dict(row)

    async def dismiss(
        self, user_id: str, card_id: str, *, session_id: str
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            row = await self._get_row(db, user_id, card_id, session_id=session_id)
            if row.status not in _SETTLEMENT_ACTIONABLE:
                raise ValueError(f"settlement card is not actionable: {card_id}")
            row.status = "dismissed"
            row.updated_at = utcnow()
            await db.commit()
            return _settlement_to_dict(row)

    async def delete_if_unconfirmed(
        self, user_id: str, card_id: str, *, session_id: str
    ) -> bool:
        """Delete a card not yet confirmed or dismissed; False when untouchable."""
        async with self._sessions() as db:
            row = await db.get(SettlementCardRow, card_id)
            if (
                row is None
                or row.session_id != session_id
                or row.status not in _SETTLEMENT_ACTIONABLE
                or row.user_id != user_id
            ):
                return False
            await db.delete(row)
            await db.commit()
            return True


class SQLChannelConfigStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load(self, user_id: str) -> dict[str, Any]:
        from qwenpaw_data.host.core.channels.config import initial_config

        async with self._sessions() as db:
            row = await db.get(ChannelConfigRow, user_id)
            if row is not None and row.config_json:
                return dict(row.config_json)
        return initial_config()

    async def save(self, user_id: str, config: dict[str, Any]) -> None:
        async with self._sessions() as db:
            row = await db.get(ChannelConfigRow, user_id)
            if row is None:
                db.add(
                    ChannelConfigRow(
                        user_id=user_id,
                        config_json=dict(config),
                        updated_at=utcnow(),
                    )
                )
            else:
                row.config_json = dict(config)
                row.updated_at = utcnow()
            await db.commit()

    async def list_user_ids(self) -> list[str]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ChannelConfigRow).order_by(ChannelConfigRow.user_id)
                )
            ).all()
            return [r.user_id for r in rows]


def _binding_to_dict(row: ChannelBindingRow) -> dict[str, Any]:
    return {
        "external_key": row.external_key,
        "target_type": row.target_type,
        "display_name": row.display_name,
        "last_active_at": row.updated_at,
    }


class SQLChannelBindingStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_active_session_id(
        self, user_id: str, channel: str, external_key: str
    ) -> str | None:
        async with self._sessions() as db:
            row = await db.get(ChannelBindingRow, (user_id, channel, external_key))
            return row.active_session_id if row is not None else None

    async def point_to(
        self,
        user_id: str,
        channel: str,
        external_key: str,
        session_id: str,
        *,
        target_meta: dict[str, Any] | None = None,
        display_name: str = "",
    ) -> None:
        now = utcnow()
        async with self._sessions() as db:
            row = await db.get(ChannelBindingRow, (user_id, channel, external_key))
            if row is None:
                db.add(
                    ChannelBindingRow(
                        user_id=user_id,
                        channel=channel,
                        external_key=external_key,
                        active_session_id=session_id,
                        target_type=str((target_meta or {}).get("target_type") or ""),
                        display_name=display_name,
                        send_meta_json=(target_meta or {}).get("send_meta") or {},
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.active_session_id = session_id
                if target_meta:
                    row.target_type = str(target_meta.get("target_type") or "")
                    row.send_meta_json = target_meta.get("send_meta") or {}
                if display_name:
                    row.display_name = display_name
                row.updated_at = now
            await db.commit()

    async def list_by_channel(
        self, user_id: str, channel: str
    ) -> list[dict[str, Any]]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ChannelBindingRow)
                    .where(
                        ChannelBindingRow.user_id == user_id,
                        ChannelBindingRow.channel == channel,
                    )
                    .order_by(ChannelBindingRow.updated_at.desc())
                )
            ).all()
            return [_binding_to_dict(r) for r in rows]

    async def get_target_meta(
        self, user_id: str, channel: str, external_key: str
    ) -> dict[str, Any] | None:
        async with self._sessions() as db:
            row = await db.get(ChannelBindingRow, (user_id, channel, external_key))
            if row is None:
                return None
            return {
                "target_type": row.target_type,
                "display_name": row.display_name,
                "send_meta": row.send_meta_json or {},
            }

    async def exists(self, user_id: str, channel: str, external_key: str) -> bool:
        async with self._sessions() as db:
            return (
                await db.get(ChannelBindingRow, (user_id, channel, external_key))
            ) is not None


def _attachment_from_row(row: AttachmentRow) -> Attachment:
    return Attachment(
        id=row.attachment_id,
        session_id=row.session_id,
        identity=Identity(user_id=row.user_id),
        filename=row.filename,
        storage_path=row.storage_path,
        created_at=row.created_at,
    )


class SQLAttachmentStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, attachment: Attachment) -> None:
        async with self._sessions() as db:
            existing = await db.get(AttachmentRow, attachment.id)
            if existing is not None:
                raise RuntimeError(
                    f"CONFLICT: attachment already exists: {attachment.id}"
                )
            db.add(
                AttachmentRow(
                    attachment_id=attachment.id,
                    user_id=attachment.identity.user_id,
                    session_id=attachment.session_id,
                    filename=attachment.filename,
                    storage_path=attachment.storage_path,
                    created_at=attachment.created_at,
                )
            )
            await db.commit()

    async def get(self, user_id: str, attachment_id: str) -> Attachment:
        async with self._sessions() as db:
            row = await db.get(AttachmentRow, attachment_id)
            if row is None or row.user_id != user_id:
                raise LookupError("attachment not found")
            return _attachment_from_row(row)

    async def find_by_filename(
        self,
        user_id: str,
        session_id: str,
        filename: str,
    ) -> Attachment | None:
        async with self._sessions() as db:
            row = (
                await db.scalars(
                    select(AttachmentRow).where(
                        AttachmentRow.session_id == session_id,
                        AttachmentRow.filename == filename,
                        AttachmentRow.user_id == user_id,
                    )
                )
            ).first()
            return _attachment_from_row(row) if row is not None else None

    async def require_for_session(
        self,
        user_id: str,
        session_id: str,
        attachment_ids: list[str],
        *,
        workspace: Path,
    ) -> list[Attachment]:
        seen: set[str] = set()
        items: list[Attachment] = []
        for attachment_id in attachment_ids:
            if attachment_id in seen:
                raise ValueError(f"duplicate attachment_id: {attachment_id}")
            seen.add(attachment_id)
            attachment = await self.get(user_id, attachment_id)
            if attachment.session_id != session_id:
                raise LookupError("attachment not found")
            attachment.require_file(workspace)
            items.append(attachment)
        return items

    async def delete(self, user_id: str, attachment_id: str) -> None:
        async with self._sessions() as db:
            row = await db.get(AttachmentRow, attachment_id)
            if row is None or row.user_id != user_id:
                raise LookupError("attachment not found")
            await db.delete(row)
            await db.commit()
