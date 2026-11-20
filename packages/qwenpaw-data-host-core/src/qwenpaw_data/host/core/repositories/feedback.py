# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import FeedbackRow
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import (
    tracer_clear_reaction,
    tracer_send_feedback,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


class FeedbackRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    @staticmethod
    def _to_dict(row: FeedbackRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "session_id": row.session_id,
            "chat_id": row.chat_id,
            "kind": row.kind,
            "reason": row.reason,
            "detail": row.detail,
            "artifact_ref": row.artifact_ref_json,
            "created_at": row.created_at,
        }

    async def _reaction_rows(self, chat: Chat) -> list[FeedbackRow]:
        return list(
            (
                await self.db.scalars(
                    select(FeedbackRow)
                    .where(
                        FeedbackRow.user_id == self.identity.user_id,
                        FeedbackRow.tenant_id == self.identity.tenant_id,
                        FeedbackRow.workspace_id == self.identity.workspace_id,
                        FeedbackRow.session_id == chat.session_id,
                        FeedbackRow.chat_id == chat.id,
                        FeedbackRow.kind.in_(("like", "dislike")),
                    )
                    .order_by(FeedbackRow.created_at.desc(), FeedbackRow.id.desc())
                )
            ).all()
        )

    async def get_reaction(self, chat: Chat) -> dict[str, Any] | None:
        rows = await self._reaction_rows(chat)
        return self._to_dict(rows[0]) if rows else None

    async def set_reaction(
        self,
        chat: Chat,
        *,
        kind: str,
        reason: str | None,
        detail: str | None,
    ) -> dict[str, Any]:
        if kind not in {"like", "dislike"}:
            raise ValueError("reaction kind must be like or dislike")

        now = utcnow()
        rows = await self._reaction_rows(chat)
        if rows:
            row = rows[0]
            row.kind = kind
            row.reason = reason
            row.detail = detail
            row.artifact_ref_json = None
            row.created_at = now
            for stale in rows[1:]:
                await self.db.delete(stale)
        else:
            row = FeedbackRow(
                id=create_id("fbk"),
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
                workspace_id=self.identity.workspace_id,
                session_id=chat.session_id,
                chat_id=chat.id,
                kind=kind,
                reason=reason,
                detail=detail,
                artifact_ref_json=None,
                created_at=now,
            )
            self.db.add(row)

        chat.updated_at = now
        await self.db.flush()
        recorded = self._to_dict(row)
        await tracer_send_feedback(self.db, self.identity, recorded)
        return recorded

    async def clear_reaction(self, chat: Chat) -> None:
        rows = await self._reaction_rows(chat)
        if not rows:
            return
        for row in rows:
            await tracer_clear_reaction(self.db, self.identity, row.id)
            await self.db.delete(row)
        chat.updated_at = utcnow()
        await self.db.flush()

    async def add(
        self,
        chat: Chat,
        *,
        kind: str,
        reason: str | None,
        detail: str | None,
        artifact_ref: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = utcnow()
        row = FeedbackRow(
            id=create_id("fbk"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            session_id=chat.session_id,
            chat_id=chat.id,
            kind=kind,
            reason=reason,
            detail=detail,
            artifact_ref_json=artifact_ref,
            created_at=now,
        )
        self.db.add(row)
        chat.updated_at = now
        await self.db.flush()
        recorded = self._to_dict(row)
        await tracer_send_feedback(self.db, self.identity, recorded)
        return recorded
