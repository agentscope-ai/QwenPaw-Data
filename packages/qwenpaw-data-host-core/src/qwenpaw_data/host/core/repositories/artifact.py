# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ArtifactRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


class ArtifactRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _to_dict(self, row: ArtifactRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "session_id": row.session_id,
            "chat_id": row.chat_id,
            "name": row.name,
            "path": row.path,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def register(self, **fields: Any) -> dict[str, Any]:
        """Record one saved file; rewriting the same path in a Chat updates that row."""
        now = utcnow()
        chat_id = fields.get("chat_id")
        path = fields["path"]
        row = await self._find_by_path(chat_id, path)
        if row is not None:
            row.name = fields["name"]
            row.updated_at = now
            await self.db.flush()
            return self._to_dict(row)
        row = ArtifactRow(
            id=fields.get("id") or create_id("art"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            session_id=fields["session_id"],
            chat_id=chat_id,
            name=fields["name"],
            path=path,
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        await self.db.flush()
        return self._to_dict(row)

    async def _find_by_path(self, chat_id: str | None, path: str) -> ArtifactRow | None:
        if not chat_id:
            return None
        return await self.db.scalar(
            select(ArtifactRow)
            .where(ArtifactRow.chat_id == chat_id, ArtifactRow.path == path)
            .order_by(ArtifactRow.created_at)
            .limit(1)
        )

    async def get(
        self,
        artifact_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        row = await self.db.get(ArtifactRow, artifact_id)
        if row is None:
            raise LookupError("artifact not found")
        if session_id is not None and row.session_id != session_id:
            raise LookupError("artifact not found")
        return self._to_dict(row)

    async def list_for_chats(
        self, chat_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not chat_ids:
            return {}
        rows = (
            await self.db.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.chat_id.in_(chat_ids))
                .order_by(ArtifactRow.created_at)
            )
        ).all()
        out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in chat_ids}
        for row in rows:
            if row.chat_id is None:
                continue
            out.setdefault(row.chat_id, []).append(self._to_dict(row))
        return out

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            raise ValueError("session_id is required")
        rows = (
            await self.db.scalars(
                select(ArtifactRow)
                .where(
                    ArtifactRow.session_id == session_id,
                    ArtifactRow.chat_id.is_not(None),
                )
                .order_by(ArtifactRow.updated_at.desc())
            )
        ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.path in latest:
                continue
            latest[row.path] = self._to_dict(row)
        return list(latest.values())
