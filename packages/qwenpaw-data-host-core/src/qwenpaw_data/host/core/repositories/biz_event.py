# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import BizEventRow
from qwenpaw_data.host.core.domain.identity import Identity


class BizEventRepository:
    """BizEvent 持久化：event_id 为 upsert 键，同 id 重发时覆盖旧记录。"""

    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    @staticmethod
    def _to_dict(row: BizEventRow) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "chat_id": row.chat_id,
            "seq": row.seq,
            "channel": row.channel,
            "block_id": row.block_id,
            "status": row.status,
            "presentation": row.presentation_json,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }

    async def upsert(
        self,
        *,
        chat_id: str,
        event_id: str,
        seq: int,
        channel: str = "main",
        block_id: str | None = None,
        status: str = "done",
        presentation: dict[str, Any] | None = None,
        started_at: float = 0.0,
        ended_at: float | None = None,
    ) -> dict[str, Any]:
        row = await self.db.get(BizEventRow, (chat_id, event_id))
        if row is None:
            row = BizEventRow(
                event_id=event_id,
                chat_id=chat_id,
                seq=seq,
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
                workspace_id=self.identity.workspace_id,
            )
            self.db.add(row)
        row.channel = channel
        row.block_id = block_id
        row.status = status
        row.presentation_json = presentation
        row.started_at = started_at
        row.ended_at = ended_at
        await self.db.flush()
        return self._to_dict(row)

    async def list_for_chats(
        self, chat_ids: list[str], *, main_only: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        if not chat_ids:
            return {}
        q = select(BizEventRow).where(BizEventRow.chat_id.in_(chat_ids))
        if main_only:
            q = q.where(BizEventRow.channel == "main")
        rows = (await self.db.scalars(q.order_by(BizEventRow.seq))).all()
        out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in chat_ids}
        for row in rows:
            out.setdefault(row.chat_id, []).append(self._to_dict(row))
        return out

    async def list_for_chat(
        self, chat_id: str, *, main_only: bool = True
    ) -> list[dict[str, Any]]:
        return (await self.list_for_chats([chat_id], main_only=main_only)).get(
            chat_id, []
        )
