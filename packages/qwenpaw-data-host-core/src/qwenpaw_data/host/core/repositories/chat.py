# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from agentscope.state import TaskContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.db.tables import ChatEventRow, ChatRow
from qwenpaw_data.host.core.domain.chat import ACTIVE, Chat
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import (
    tracer_send_followup_adoption,
    tracer_send_status,
)
from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.status import chat_status_from_response
from qwenpaw_data.host.core.utils.time import utcnow


class ChatRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    @staticmethod
    def _from_row(row: ChatRow) -> Chat:
        return Chat(
            id=row.id,
            session_id=row.session_id,
            identity=Identity(
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
            ),
            sequence=row.sequence,
            user_input=row.user_input,
            datasource_id=row.datasource_id,
            dataset_names=list(row.dataset_names) if row.dataset_names else None,
            kind=row.kind,
            status=row.status,
            last_sequence_number=row.last_sequence_number,
            started_at=row.started_at,
            completed_at=row.completed_at,
            active_duration_ms=row.active_duration_ms,
            error=row.error_json,
            plan=TaskContext.model_validate(row.plan or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
            artifact_comments=list(row.artifact_comments_json or []),
            attachments=list(row.attachments_json or []),
            agent_state=None
            if row.agent_state_json is None
            else dict(row.agent_state_json),
            enable_clarification=row.enable_clarification,
            heartbeat_at=row.heartbeat_at,
            otel_span_id=row.otel_span_id,
        )

    @classmethod
    async def recover_running(cls, db: AsyncSession) -> int:
        recovered = 0
        rows = (
            await db.scalars(select(ChatRow).where(ChatRow.status.in_(ACTIVE)))
        ).all()
        for row in rows:
            chat = cls._from_row(row)
            repo = cls(db, chat.identity)
            events = (
                await db.scalars(
                    select(ChatEventRow)
                    .where(
                        ChatEventRow.chat_id == row.id,
                        ChatEventRow.object == "response",
                    )
                    .order_by(ChatEventRow.sequence_number)
                )
            ).all()
            status = None
            for event in events:
                status = chat_status_from_response((event.payload or {}).get("status"))
                if status is not None:
                    break
            if status is not None:
                chat.mark_status(status)
                await repo.save(chat)
                await ChatCommandRepository(db).clear(chat.id)
                await tracer_send_status(db, chat)
                recovered += 1
            elif not chat.is_heartbeat_fresh():
                await repo.stop(chat)
                recovered += 1
        return recovered

    def _apply(self, row: ChatRow, chat: Chat) -> None:
        row.user_input = chat.user_input
        row.datasource_id = chat.datasource_id
        row.dataset_names = chat.dataset_names
        row.kind = chat.kind
        row.status = chat.status
        row.started_at = chat.started_at
        row.completed_at = chat.completed_at
        row.active_duration_ms = chat.active_duration_ms
        row.error_json = chat.error
        row.plan = chat.plan.model_dump(mode="json")
        row.artifact_comments_json = chat.artifact_comments
        row.attachments_json = chat.attachments
        row.agent_state_json = chat.agent_state
        row.enable_clarification = chat.enable_clarification
        row.heartbeat_at = chat.heartbeat_at
        # The trace id derives from the chat id, so only the span id is stored.
        if chat.otel_span_id:
            row.otel_span_id = chat.otel_span_id
        row.updated_at = chat.updated_at

    async def add(self, chat: Chat, *, inherit_plan: bool = False) -> None:
        if inherit_plan:
            payload = await self.db.scalar(
                select(ChatRow.plan)
                .where(ChatRow.session_id == chat.session_id)
                .order_by(ChatRow.sequence.desc())
                .limit(1)
            )
            if payload:
                plan = TaskContext.model_validate(payload)
                if plan.tasks and any(task.state != "completed" for task in plan.tasks):
                    chat.plan = plan
        self.db.add(
            ChatRow(
                id=chat.id,
                session_id=chat.session_id,
                user_id=chat.identity.user_id,
                tenant_id=chat.identity.tenant_id,
                workspace_id=chat.identity.workspace_id,
                sequence=chat.sequence,
                user_input=chat.user_input,
                datasource_id=chat.datasource_id,
                dataset_names=chat.dataset_names,
                kind=chat.kind,
                status=chat.status,
                last_sequence_number=chat.last_sequence_number,
                started_at=chat.started_at,
                completed_at=chat.completed_at,
                active_duration_ms=chat.active_duration_ms,
                error_json=chat.error,
                plan=chat.plan.model_dump(mode="json"),
                artifact_comments_json=chat.artifact_comments,
                attachments_json=chat.attachments,
                agent_state_json=chat.agent_state,
                enable_clarification=chat.enable_clarification,
                heartbeat_at=chat.heartbeat_at,
                otel_span_id=chat.otel_span_id,
                created_at=chat.created_at,
                updated_at=chat.updated_at,
            )
        )
        try:
            await self.db.flush()
        except IntegrityError as exc:
            if "ux_chats_session_running" not in str(exc.orig):
                raise
            raise RuntimeError("CONFLICT: session already has an active chat") from exc
        await tracer_send_followup_adoption(self.db, chat)

    async def get(
        self,
        chat_id: str,
        *,
        session_id: str | None = None,
        populate_existing: bool = False,
    ) -> Chat:
        row = await self.db.scalar(
            select(ChatRow)
            .where(ChatRow.id == chat_id)
            .execution_options(populate_existing=populate_existing)
        )
        if row is None:
            raise LookupError("chat not found")
        if session_id is not None and row.session_id != session_id:
            raise LookupError("chat not found")
        return self._from_row(row)

    async def touch_heartbeat(self, chat_id: str) -> None:
        row = await self.db.get(ChatRow, chat_id)
        if row is None:
            raise LookupError("chat not found")
        row.heartbeat_at = utcnow()
        await self.db.flush()

    async def stop(self, chat: Chat) -> Chat:
        chat = await self.get(chat.id, populate_existing=True)
        if not chat.is_active():
            return chat
        if chat.is_heartbeat_fresh():
            await ChatCommandRepository(self.db).add(ChatCommand.cancel(chat))
            return chat
        chat.cancel()
        await self.save(chat)
        await ChatCommandRepository(self.db).clear(chat.id)
        await tracer_send_status(self.db, chat, "canceled")
        await self.db.commit()
        await OutputStream(
            get_session_factory(),
            session_id=chat.session_id,
            chat_id=chat.id,
            identity=chat.identity,
            last_sequence_number=chat.last_sequence_number,
        ).response_cancelled()
        return chat

    async def get_active(self, session_id: str) -> Chat | None:
        row = await self.db.scalar(
            select(ChatRow).where(
                ChatRow.session_id == session_id,
                ChatRow.status.in_(ACTIVE),
            )
        )
        return self._from_row(row) if row is not None else None

    async def reload_event_watermark(self, chat: Chat) -> None:
        row = await self.db.scalar(
            select(ChatRow)
            .where(ChatRow.id == chat.id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise LookupError("chat not found")
        chat.apply_event_watermark(
            last_sequence_number=row.last_sequence_number,
            updated_at=row.updated_at,
        )

    async def save(self, chat: Chat) -> None:
        row = await self.db.get(ChatRow, chat.id)
        if row is None:
            raise LookupError("chat not found")
        self._apply(row, chat)
        await self.db.flush()

    async def update_plan(self, chat_id: str, plan: TaskContext) -> None:
        row = await self.db.get(ChatRow, chat_id)
        if row is None:
            raise LookupError("chat not found")
        row.plan = plan.model_dump(mode="json")
        row.updated_at = utcnow()
        await self.db.flush()

    async def list_for_session(
        self,
        session_id: str,
        *,
        before_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[Chat]:
        stmt = select(ChatRow).where(ChatRow.session_id == session_id)
        if before_sequence is not None:
            stmt = stmt.where(ChatRow.sequence < before_sequence)
        stmt = stmt.order_by(ChatRow.sequence.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.db.scalars(stmt)).all()
        return [self._from_row(row) for row in reversed(rows)]

    async def get_active_for_session(self, session_id: str) -> Chat | None:
        row = (
            await self.db.scalars(
                select(ChatRow)
                .where(
                    ChatRow.session_id == session_id,
                    ChatRow.status.in_(ACTIVE),
                )
                .order_by(ChatRow.sequence.desc())
            )
        ).first()
        return self._from_row(row) if row is not None else None

    async def read_messages(self, chat_id: str) -> list[dict[str, Any]]:
        evs = (
            await self.db.scalars(
                select(ChatEventRow)
                .where(
                    ChatEventRow.chat_id == chat_id, ChatEventRow.object == "message"
                )
                .order_by(ChatEventRow.sequence_number)
            )
        ).all()
        messages: list[dict[str, Any]] = []
        for e in evs:
            payload = dict(e.payload)
            payload.pop("sequence_number", None)
            payload.pop("session_id", None)
            payload.pop("created_at", None)
            messages.append(payload)
        return messages
