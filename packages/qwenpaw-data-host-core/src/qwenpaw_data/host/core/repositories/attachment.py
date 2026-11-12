# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import AttachmentRow
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.identity import Identity


class AttachmentRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _from_row(self, row: AttachmentRow) -> Attachment:
        return Attachment(
            id=row.attachment_id,
            session_id=row.session_id,
            identity=Identity(
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
            ),
            filename=row.filename,
            storage_path=row.storage_path,
            created_at=row.created_at,
        )

    def _owned(self, row: AttachmentRow | None) -> Attachment:
        if (
            row is None
            or row.user_id != self.identity.user_id
            or row.tenant_id != self.identity.tenant_id
            or row.workspace_id != self.identity.workspace_id
        ):
            raise LookupError("attachment not found")
        return self._from_row(row)

    async def add(self, attachment: Attachment) -> None:
        self.db.add(
            AttachmentRow(
                attachment_id=attachment.id,
                user_id=attachment.identity.user_id,
                tenant_id=attachment.identity.tenant_id,
                workspace_id=attachment.identity.workspace_id,
                session_id=attachment.session_id,
                filename=attachment.filename,
                storage_path=attachment.storage_path,
                created_at=attachment.created_at,
            )
        )
        await self.db.flush()

    async def get(self, attachment_id: str) -> Attachment:
        row = await self.db.get(AttachmentRow, attachment_id)
        return self._owned(row)

    async def find_by_filename(
        self, session_id: str, filename: str
    ) -> Attachment | None:
        row = await self.db.scalar(
            select(AttachmentRow).where(
                AttachmentRow.session_id == session_id,
                AttachmentRow.filename == filename,
                AttachmentRow.user_id == self.identity.user_id,
                AttachmentRow.tenant_id == self.identity.tenant_id,
                AttachmentRow.workspace_id == self.identity.workspace_id,
            )
        )
        return self._from_row(row) if row is not None else None

    async def require_for_session(
        self,
        session_id: str,
        attachment_ids: list[str],
        *,
        workspace: Path,
    ) -> list[Attachment]:
        seen: set[str] = set()
        items: list[Attachment] = []
        for attachment_id in attachment_ids:
            if attachment_id in seen:
                raise ValueError(f"duplicate attachment_id: {attachment_id}")
            seen.add(attachment_id)
            attachment = await self.get(attachment_id)
            if attachment.session_id != session_id:
                raise LookupError("attachment not found")
            attachment.require_file(workspace)
            items.append(attachment)
        return items

    async def delete(self, attachment: Attachment) -> None:
        row = await self.db.get(AttachmentRow, attachment.id)
        self._owned(row)
        await self.db.delete(row)
        await self.db.flush()
