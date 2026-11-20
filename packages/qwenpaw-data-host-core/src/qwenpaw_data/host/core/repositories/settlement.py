# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import SettlementCardRow, SettlementCardStatus
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow

_ACTIONABLE = frozenset({"pending", "queried"})


class SettlementRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _to_dict(self, row: SettlementCardRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "workspace_id": row.workspace_id,
            "session_id": row.session_id,
            "source_chat_id": row.source_chat_id,
            "type": row.type,
            "fields": dict(row.fields_json or {}),
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "confirmed_at": row.confirmed_at,
        }

    async def add(
        self,
        *,
        session_id: str,
        source_chat_id: str,
        type: str,
        fields: dict[str, str],
    ) -> dict[str, Any]:
        now = utcnow()
        row = SettlementCardRow(
            id=create_id("card"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            session_id=session_id,
            source_chat_id=source_chat_id,
            type=type,
            fields_json=dict(fields),
            status="pending",
            created_at=now,
            updated_at=now,
            confirmed_at=None,
        )
        self.db.add(row)
        await self.db.flush()
        return self._to_dict(row)

    async def list_by_session(
        self,
        session_id: str,
        status: SettlementCardStatus | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(SettlementCardRow).where(
            SettlementCardRow.session_id == session_id,
            SettlementCardRow.user_id == self.identity.user_id,
            SettlementCardRow.tenant_id == self.identity.tenant_id,
            SettlementCardRow.workspace_id == self.identity.workspace_id,
        )
        if status is not None:
            stmt = stmt.where(SettlementCardRow.status == status)
        stmt = stmt.order_by(SettlementCardRow.created_at.desc())
        rows = (await self.db.scalars(stmt)).all()
        return [self._to_dict(row) for row in rows]

    async def list(
        self, status: SettlementCardStatus | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(SettlementCardRow).where(
            SettlementCardRow.user_id == self.identity.user_id,
            SettlementCardRow.tenant_id == self.identity.tenant_id,
            SettlementCardRow.workspace_id == self.identity.workspace_id,
        )
        if status is not None:
            stmt = stmt.where(SettlementCardRow.status == status)
        stmt = stmt.order_by(SettlementCardRow.created_at.desc())
        rows = (await self.db.scalars(stmt)).all()
        return [self._to_dict(row) for row in rows]

    async def get(self, card_id: str, *, session_id: str) -> dict[str, Any]:
        row = await self._get_row(card_id, session_id=session_id)
        return self._to_dict(row)

    async def mark_queried(self, session_id: str, card_ids: list[str]) -> None:
        if not card_ids:
            return
        now = utcnow()
        rows = (
            await self.db.scalars(
                select(SettlementCardRow).where(
                    SettlementCardRow.id.in_(card_ids),
                    SettlementCardRow.session_id == session_id,
                    SettlementCardRow.status == "pending",
                    SettlementCardRow.user_id == self.identity.user_id,
                    SettlementCardRow.tenant_id == self.identity.tenant_id,
                    SettlementCardRow.workspace_id == self.identity.workspace_id,
                )
            )
        ).all()
        for row in rows:
            row.status = "queried"
            row.updated_at = now
        await self.db.flush()

    async def confirm(
        self,
        card_id: str,
        *,
        session_id: str,
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        row = await self._get_row(card_id, session_id=session_id)
        if row.status not in _ACTIONABLE:
            raise ValueError(f"settlement card is not actionable: {card_id}")
        now = utcnow()
        if fields is not None:
            row.fields_json = dict(fields)
        row.status = "confirmed"
        row.confirmed_at = now
        row.updated_at = now
        await self.db.flush()
        return self._to_dict(row)

    async def dismiss(self, card_id: str, *, session_id: str) -> dict[str, Any]:
        row = await self._get_row(card_id, session_id=session_id)
        if row.status not in _ACTIONABLE:
            raise ValueError(f"settlement card is not actionable: {card_id}")
        row.status = "dismissed"
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row)

    async def delete_if_unconfirmed(self, card_id: str, *, session_id: str) -> bool:
        """删除尚未确认或拒绝的卡片；找不到、身份不对或已处理则返回 False。"""
        row = await self.db.get(SettlementCardRow, card_id)
        if (
            row is None
            or row.session_id != session_id
            or row.status not in _ACTIONABLE
            or row.user_id != self.identity.user_id
            or row.tenant_id != self.identity.tenant_id
            or row.workspace_id != self.identity.workspace_id
        ):
            return False
        await self.db.delete(row)
        await self.db.flush()
        return True

    async def _get_row(self, card_id: str, *, session_id: str) -> SettlementCardRow:
        row = await self.db.get(SettlementCardRow, card_id)
        if (
            row is None
            or row.session_id != session_id
            or row.user_id != self.identity.user_id
            or row.tenant_id != self.identity.tenant_id
            or row.workspace_id != self.identity.workspace_id
        ):
            raise LookupError(f"settlement card not found: {card_id}")
        return row
