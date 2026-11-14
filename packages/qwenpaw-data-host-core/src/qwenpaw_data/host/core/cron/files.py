# -*- coding: utf-8 -*-
"""Job-scoped file staging until a Session exists to receive attachments."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.files import write_file_to_dir
from qwenpaw_data.host.core.utils.safe_name import require_safe_name


class JobFiles:
    def __init__(self, identity: Identity, job_id: str) -> None:
        self._identity = identity
        self._home = get_settings().paths.home.resolve()
        self._root = (
            self._home
            / require_safe_name(identity.tenant_id)
            / require_safe_name(identity.workspace_id)
            / require_safe_name(identity.user_id)
            / "cron"
            / require_safe_name(job_id)
        )

    @property
    def root(self) -> Path:
        # Reject aliases into another identity's directory as well as escapes.
        if self._root.resolve() != self._root:
            raise ValueError("unsafe job attachment directory")
        return self._root

    def filenames(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_file() and not path.is_symlink())

    def add(self, filename: str, data: bytes) -> str:
        return write_file_to_dir(self.root, filename, data)

    def remove(self, filename: str) -> None:
        path = self.root / require_safe_name(filename)
        if not path.is_file():
            raise LookupError("attachment file not found")
        path.unlink()

    def clear(self) -> None:
        if self.root.is_dir():
            shutil.rmtree(self.root)

    async def copy_from_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        filenames: list[str],
    ) -> list[str]:
        from qwenpaw_data.host.core.repositories.attachment import AttachmentRepository
        from qwenpaw_data.host.core.repositories.session import SessionRepository

        source = await SessionRepository(db, self._identity).get(session_id)
        workspace = UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=self._identity,
            agent_id=source.agent_id,
            session_id=source.id,
        ).workspace
        repo = AttachmentRepository(db, self._identity)
        names: list[str] = []
        for filename in filenames:
            attachment = await repo.find_by_filename(source.id, filename)
            if attachment is None:
                raise LookupError(
                    f"attachment not found in current session: {filename}"
                )
            data = attachment.require_file(workspace).read_bytes()
            names.append(self.add(attachment.filename, data))
        return names

    async def copy_to_session(
        self, db: AsyncSession, session: Session
    ) -> list[dict[str, Any]]:
        from qwenpaw_data.host.core.repositories.attachment import AttachmentRepository

        paths = UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=self._identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )
        repo = AttachmentRepository(db, self._identity)
        refs: list[dict[str, Any]] = []
        dest_dir = paths.uploads_dir
        for filename in self.filenames():
            source = self.root / filename
            if source.is_symlink() or source.resolve().parent != self.root:
                raise ValueError("unsafe attachment file")
            data = source.read_bytes()
            name = filename
            suffix = Path(filename).suffix
            digest = sha256(filename.encode() + b"\0" + data).hexdigest()[:16]
            version = 0
            while True:
                attachment = await repo.find_by_filename(session.id, name)
                if attachment is not None:
                    if attachment.require_file(paths.workspace).read_bytes() == data:
                        break
                elif not (dest_dir / name).exists():
                    attachment = Attachment.receive(
                        session_id=session.id,
                        identity=self._identity,
                        filename=name,
                        data=data,
                        dest_dir=dest_dir,
                    )
                    await repo.add(attachment)
                    break
                # Keep the original file available to earlier chats.
                version += 1
                name = f"cron-{digest}-{version}{suffix}"
            refs.append(attachment.to_ref())
        return refs
