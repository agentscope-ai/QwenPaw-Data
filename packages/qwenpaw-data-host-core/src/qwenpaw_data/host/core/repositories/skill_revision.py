# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import SkillRevisionOrigin, SkillRevisionRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


class SkillRevisionRepository:
    """Append-only history of one agent's skill directories."""

    def __init__(self, db: AsyncSession, identity: Identity, *, agent_id: str) -> None:
        self.db = db
        self.identity = identity
        self.agent_id = agent_id

    def _to_dict(
        self, row: SkillRevisionRow, *, with_files: bool = False
    ) -> dict[str, Any]:
        item = {
            "id": row.id,
            "agent_id": row.agent_id,
            "skill_name": row.skill_name,
            "revision_no": row.revision_no,
            "origin": row.origin,
            "source_ref": row.source_ref,
            "note": row.note,
            "created_at": row.created_at,
        }
        if with_files:
            item["files"] = [dict(entry) for entry in row.files or []]
        return item

    def _scoped(
        self, stmt: Select[tuple[SkillRevisionRow]], skill_name: str
    ) -> Select[tuple[SkillRevisionRow]]:
        return stmt.where(
            SkillRevisionRow.user_id == self.identity.user_id,
            SkillRevisionRow.tenant_id == self.identity.tenant_id,
            SkillRevisionRow.workspace_id == self.identity.workspace_id,
            SkillRevisionRow.agent_id == self.agent_id,
            SkillRevisionRow.skill_name == skill_name,
        )

    async def list_history(self, skill_name: str) -> list[dict[str, Any]]:
        rows = (
            await self.db.scalars(
                self._scoped(select(SkillRevisionRow), skill_name).order_by(
                    SkillRevisionRow.revision_no.desc()
                )
            )
        ).all()
        return [self._to_dict(row) for row in rows]

    async def get(self, skill_name: str, revision_no: int) -> dict[str, Any]:
        row = (
            await self.db.scalars(
                self._scoped(select(SkillRevisionRow), skill_name).where(
                    SkillRevisionRow.revision_no == revision_no
                )
            )
        ).first()
        if row is None:
            raise LookupError(f"skill revision not found: {skill_name}@{revision_no}")
        return self._to_dict(row, with_files=True)

    async def max_revision_no(self, skill_name: str) -> int | None:
        """``None`` when the skill has no history yet."""
        return await self.db.scalar(
            self._scoped(select(func.max(SkillRevisionRow.revision_no)), skill_name)
        )

    async def append(
        self,
        skill_name: str,
        *,
        files: list[dict[str, str]],
        origin: SkillRevisionOrigin,
        source_ref: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        previous = await self.max_revision_no(skill_name)
        row = SkillRevisionRow(
            id=create_id("skillrev"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            agent_id=self.agent_id,
            skill_name=skill_name,
            revision_no=0 if previous is None else previous + 1,
            origin=origin,
            source_ref=source_ref,
            note=note,
            files=[dict(entry) for entry in files],
            created_at=utcnow(),
        )
        self.db.add(row)
        await self.db.flush()
        return self._to_dict(row, with_files=True)
