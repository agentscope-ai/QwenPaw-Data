# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    parse_stream_object,
)
from qwenpaw_data.host.core.db.tables import ChatEventRow, ChatRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.status import is_terminal_response


@dataclass(frozen=True)
class ChatEventRecord:
    chat_id: str
    sequence_number: int
    object: str
    session_id: str
    payload: dict[str, Any]
    created_at: datetime


class ChatEventRepository:
    """Persist Chat-scoped SSE / raw-trace events and sequence watermark."""

    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def persist_batch(self, events: list[ChatEventRecord]) -> None:
        if not events:
            raise ValueError("persist_batch requires events")
        await self.add_many(events)
        last = events[-1]
        chat = await self.db.get(ChatRow, last.chat_id)
        if chat is None:
            raise ValueError(f"chat not found: {last.chat_id}")
        if chat.session_id != last.session_id:
            raise ValueError("session_id mismatch")
        chat.last_sequence_number = last.sequence_number
        chat.updated_at = last.created_at
        await self.db.flush()

    async def read_after(self, chat_id: str, after: int) -> list[StreamObject]:
        result = await self.db.scalars(
            select(ChatEventRow)
            .where(
                ChatEventRow.chat_id == chat_id,
                ChatEventRow.sequence_number > after,
            )
            .order_by(ChatEventRow.sequence_number)
        )
        return [_parse_row(row) for row in result.all()]

    async def read_mcp_connections(self, chat_id: str) -> list[StreamObject]:
        rows = await self.db.scalars(
            select(ChatEventRow)
            .where(
                ChatEventRow.chat_id == chat_id, ChatEventRow.object == "mcp.connect"
            )
            .order_by(ChatEventRow.sequence_number)
        )
        return [_parse_row(row) for row in rows.all()]

    async def terminal_delivered(self, chat_id: str, cursor: int) -> bool:
        """A replay cursor is exhausted only after the durable terminal response.

        Runtime commits the Chat status before appending its final event; the
        status/watermark pair alone cannot distinguish that publication window.
        Callers must authorize access to the chat before reading its event log.
        """
        row = await self.db.scalar(
            select(ChatEventRow)
            .where(
                ChatEventRow.chat_id == chat_id,
                ChatEventRow.object == "response",
                ChatEventRow.sequence_number <= cursor,
            )
            .order_by(ChatEventRow.sequence_number.desc())
            .limit(1)
        )
        return row is not None and is_terminal_response(_parse_row(row))

    async def read_timing_events(self, chat_id: str) -> list[dict[str, Any]]:
        """Compact timing replay, including message-to-block links but no content."""
        rows = await self.db.scalars(
            select(ChatEventRow)
            .where(
                ChatEventRow.chat_id == chat_id,
                ChatEventRow.object.in_(
                    (
                        "model.call",
                        "model.call.end",
                        "tool.result.start",
                        "tool.result.end",
                        "message",
                    )
                ),
            )
            .order_by(ChatEventRow.sequence_number)
        )
        fields = {
            "object",
            "sequence_number",
            "session_id",
            "chat_id",
            "created_at",
            "id",
            "type",
            "source_id",
            "role",
            "status",
            "call_id",
            "model_name",
        }
        return [
            {
                key: value
                for key, value in _parse_row(row).model_dump(mode="json").items()
                if key in fields
            }
            for row in rows.all()
        ]

    async def last_sequence_number(
        self, chat_id: str, *, session_id: str | None = None
    ) -> int:
        chat = await self.db.get(ChatRow, chat_id)
        if chat is None:
            raise ValueError(f"chat not found: {chat_id}")
        if session_id is not None and chat.session_id != session_id:
            raise ValueError("session_id mismatch")
        return chat.last_sequence_number

    async def list_for_chats(self, chat_ids: list[str]) -> list[ChatEventRecord]:
        if not chat_ids:
            return []
        rows = (
            await self.db.scalars(
                select(ChatEventRow)
                .where(ChatEventRow.chat_id.in_(chat_ids))
                .order_by(ChatEventRow.chat_id, ChatEventRow.sequence_number)
            )
        ).all()
        return [
            ChatEventRecord(
                chat_id=row.chat_id,
                sequence_number=row.sequence_number,
                object=row.object,
                session_id=row.session_id,
                payload=dict(row.payload),
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def delete_chat_content(self, chat_id: str) -> None:
        """Delete this chat's ``content`` rows and in-progress ``message`` rows."""
        rows = (
            await self.db.scalars(
                select(ChatEventRow).where(
                    ChatEventRow.chat_id == chat_id,
                    ChatEventRow.object.in_(("content", "message")),
                )
            )
        ).all()
        for row in rows:
            if row.object == "content":
                await self.db.delete(row)
                continue
            if (row.payload or {}).get("status") == "in_progress":
                await self.db.delete(row)
        await self.db.flush()

    async def add_many(self, events: list[ChatEventRecord]) -> None:
        for event in events:
            self.db.add(
                ChatEventRow(
                    chat_id=event.chat_id,
                    sequence_number=event.sequence_number,
                    object=event.object,
                    session_id=event.session_id,
                    user_id=self.identity.user_id,
                    tenant_id=self.identity.tenant_id,
                    workspace_id=self.identity.workspace_id,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )
        await self.db.flush()


def _parse_row(row: ChatEventRow) -> StreamObject:
    payload = dict(row.payload)
    if "created_at" not in payload:
        payload["created_at"] = row.created_at
    return parse_stream_object(payload)
