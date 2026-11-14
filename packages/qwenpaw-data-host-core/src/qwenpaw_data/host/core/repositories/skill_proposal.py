# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import (
    SessionRow,
    SkillProposalRow,
    SkillProposalStatus,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


class SkillProposalRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _to_dict(
        self, row: SkillProposalRow, *, with_files: bool = False
    ) -> dict[str, Any]:
        item = {
            "id": row.id,
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "workspace_id": row.workspace_id,
            "session_id": row.session_id,
            "agent_id": row.agent_id,
            "source_chat_id": row.source_chat_id,
            "status": row.status,
            "summary": row.summary,
            "mentioned_skill_names": list(row.mentioned_skill_names or []),
            "modified_file_count": row.modified_file_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        if with_files:
            item["file_changes"] = list(row.file_changes or [])
        return item

    def _filter_by_ids(
        self, stmt: Select[tuple[SkillProposalRow]]
    ) -> Select[tuple[SkillProposalRow]]:
        return stmt.where(
            SkillProposalRow.user_id == self.identity.user_id,
            SkillProposalRow.tenant_id == self.identity.tenant_id,
            SkillProposalRow.workspace_id == self.identity.workspace_id,
        )

    async def list_by_session(
        self,
        session_id: str,
        status: SkillProposalStatus | None = None,
    ) -> list[dict[str, Any]]:
        stmt = self._filter_by_ids(
            select(SkillProposalRow).where(SkillProposalRow.session_id == session_id)
        )
        if status is not None:
            stmt = stmt.where(SkillProposalRow.status == status)
        rows = (
            await self.db.scalars(stmt.order_by(SkillProposalRow.created_at.desc()))
        ).all()
        return [self._to_dict(row) for row in rows]

    async def list(
        self, status: SkillProposalStatus | None = None
    ) -> list[dict[str, Any]]:
        stmt = self._filter_by_ids(
            select(SkillProposalRow)
            .join(SessionRow, SessionRow.id == SkillProposalRow.session_id)
            .where(SessionRow.deleted_at.is_(None))
        )
        if status is not None:
            stmt = stmt.where(SkillProposalRow.status == status)
        rows = (
            await self.db.scalars(stmt.order_by(SkillProposalRow.created_at.desc()))
        ).all()
        return [self._to_dict(row) for row in rows]

    async def get(self, proposal_id: str, *, session_id: str) -> dict[str, Any]:
        return self._to_dict(
            await self._get_row(proposal_id, session_id=session_id),
            with_files=True,
        )

    async def upsert_pending(
        self,
        *,
        session_id: str,
        agent_id: str,
        summary: str,
        mentioned_skill_names: list[str],
        modified_file_count: int,
        file_changes: list[dict[str, Any]],
        source_chat_id: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        names = list(mentioned_skill_names)
        changes = [dict(item) for item in file_changes]
        pending = (
            await self.db.scalars(
                self._filter_by_ids(
                    select(SkillProposalRow).where(
                        SkillProposalRow.session_id == session_id,
                        SkillProposalRow.status == "pending",
                    )
                ).order_by(SkillProposalRow.created_at.desc())
            )
        ).all()
        if pending:
            row = pending[0]
            row.agent_id = agent_id
            row.summary = summary
            row.mentioned_skill_names = names
            row.modified_file_count = modified_file_count
            row.file_changes = changes
            if source_chat_id:
                row.source_chat_id = source_chat_id
            row.updated_at = now
            for extra in pending[1:]:
                await self.db.delete(extra)
            await self.db.flush()
            return self._to_dict(row, with_files=True)

        row = SkillProposalRow(
            id=create_id("proposal"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            session_id=session_id,
            agent_id=agent_id,
            source_chat_id=source_chat_id,
            status="pending",
            summary=summary,
            mentioned_skill_names=names,
            modified_file_count=modified_file_count,
            file_changes=changes,
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def save_accepted(
        self,
        proposal_id: str,
        *,
        session_id: str,
        status: SkillProposalStatus,
        file_changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        row = await self._get_row(proposal_id, session_id=session_id)
        row.status = status
        row.file_changes = list(file_changes)
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def reject(self, proposal_id: str, *, session_id: str) -> dict[str, Any]:
        row = await self._get_row(proposal_id, session_id=session_id)
        if row.status != "pending":
            raise ValueError(f"skill proposal is not pending: {proposal_id}")
        row.status = "rejected"
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def _get_row(self, proposal_id: str, *, session_id: str) -> SkillProposalRow:
        row = (
            await self.db.scalars(
                self._filter_by_ids(
                    select(SkillProposalRow).where(
                        SkillProposalRow.id == proposal_id,
                        SkillProposalRow.session_id == session_id,
                    )
                )
            )
        ).first()
        if row is None:
            raise LookupError(f"skill proposal not found: {proposal_id}")
        return row
