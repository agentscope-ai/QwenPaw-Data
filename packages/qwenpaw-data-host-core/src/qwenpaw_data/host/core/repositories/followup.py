# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import FollowUpRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.time import utcnow


class FollowUpRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _to_dict(self, row: FollowUpRow) -> dict[str, Any]:
        return {
            "chat_id": row.chat_id,
            "questions": list(row.questions_json or []),
        }

    async def upsert(
        self,
        *,
        chat_id: str,
        questions: list[str],
    ) -> dict[str, Any]:
        row = await self.db.get(FollowUpRow, chat_id)
        if row is None:
            row = FollowUpRow(
                chat_id=chat_id,
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
                workspace_id=self.identity.workspace_id,
                questions_json=questions,
                created_at=utcnow(),
            )
            self.db.add(row)
        else:
            row.questions_json = questions
        await self.db.flush()
        return self._to_dict(row)

    async def list_for_chats(
        self, chat_ids: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        if not chat_ids:
            return {}
        rows = (
            await self.db.scalars(
                select(FollowUpRow).where(FollowUpRow.chat_id.in_(chat_ids))
            )
        ).all()
        out: dict[str, dict[str, Any] | None] = {cid: None for cid in chat_ids}
        for row in rows:
            out[row.chat_id] = self._to_dict(row)
        return out
