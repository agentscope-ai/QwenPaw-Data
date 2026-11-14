# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import SkillDraftRow, SkillDraftStatus
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.skill import parse_skill_frontmatter_text
from qwenpaw_data.host.core.utils.time import utcnow


def _skill_name(files: dict[str, str]) -> str:
    content = files.get("SKILL.md")
    if not content:
        return ""
    raw = parse_skill_frontmatter_text(content, source="SKILL.md").get("name")
    return raw.strip() if isinstance(raw, str) else ""


class SkillDraftRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _to_dict(self, row: SkillDraftRow, *, with_files: bool) -> dict[str, Any]:
        files = dict(row.files_json or {})
        item = {
            "id": row.id,
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "workspace_id": row.workspace_id,
            "session_id": row.session_id,
            "source_chat_id": row.source_chat_id,
            "status": row.status,
            "name": _skill_name(files),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        if with_files:
            item["files"] = files
        return item

    async def add(self, *, session_id: str, source_chat_id: str) -> dict[str, Any]:
        now = utcnow()
        row = SkillDraftRow(
            id=create_id("draft"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            session_id=session_id,
            source_chat_id=source_chat_id,
            status="generating",
            files_json={},
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.db.scalars(
                select(SkillDraftRow)
                .where(
                    SkillDraftRow.session_id == session_id,
                    SkillDraftRow.user_id == self.identity.user_id,
                    SkillDraftRow.tenant_id == self.identity.tenant_id,
                    SkillDraftRow.workspace_id == self.identity.workspace_id,
                )
                .order_by(SkillDraftRow.created_at.desc())
            )
        ).all()
        return [self._to_dict(row, with_files=False) for row in rows]

    async def get(self, draft_id: str, *, session_id: str) -> dict[str, Any]:
        return self._to_dict(
            await self._get_row(draft_id, session_id=session_id),
            with_files=True,
        )

    async def save_files(
        self,
        draft_id: str,
        *,
        session_id: str,
        files: dict[str, str],
        status: SkillDraftStatus,
    ) -> dict[str, Any]:
        row = await self._get_row(draft_id, session_id=session_id)
        row.files_json = dict(files)
        row.status = status
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def mark_status(
        self,
        draft_id: str,
        *,
        session_id: str,
        status: SkillDraftStatus,
        expected_status: SkillDraftStatus | None = None,
    ) -> dict[str, Any]:
        row = await self._get_row(draft_id, session_id=session_id)
        if expected_status is not None and row.status != expected_status:
            raise ValueError(f"skill draft is not {expected_status}: {draft_id}")
        row.status = status
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def dismiss(self, draft_id: str, *, session_id: str) -> dict[str, Any]:
        row = await self._get_row(draft_id, session_id=session_id)
        if row.status == "accepted":
            raise ValueError(f"skill draft is already accepted: {draft_id}")
        row.status = "dismissed"
        row.updated_at = utcnow()
        await self.db.flush()
        return self._to_dict(row, with_files=True)

    async def _get_row(self, draft_id: str, *, session_id: str) -> SkillDraftRow:
        row = await self.db.get(SkillDraftRow, draft_id)
        if (
            row is None
            or row.session_id != session_id
            or row.user_id != self.identity.user_id
            or row.tenant_id != self.identity.tenant_id
            or row.workspace_id != self.identity.workspace_id
        ):
            raise LookupError(f"skill draft not found: {draft_id}")
        return row
