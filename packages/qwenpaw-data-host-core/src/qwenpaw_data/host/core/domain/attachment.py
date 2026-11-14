# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.files import write_file_to_dir
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


def uploads_relative_path(session_id: str, filename: str) -> str:
    return f"uploads/{session_id}/{filename}"


@dataclass
class Attachment:
    id: str
    session_id: str
    identity: Identity
    filename: str
    storage_path: str
    created_at: datetime

    @classmethod
    def receive(
        cls,
        *,
        session_id: str,
        identity: Identity,
        filename: str,
        data: bytes,
        dest_dir: Path,
    ) -> Attachment:
        name = write_file_to_dir(dest_dir, filename, data)
        return cls(
            id=create_id("att"),
            session_id=session_id,
            identity=identity,
            filename=name,
            storage_path=uploads_relative_path(session_id, name),
            created_at=utcnow(),
        )

    def to_ref(self) -> dict[str, Any]:
        return {"attachment_id": self.id, "filename": self.filename}

    def require_file(self, workspace: Path) -> Path:
        path = (workspace / self.storage_path).resolve()
        root = workspace.resolve()
        if not path.is_relative_to(root):
            raise ValueError("attachment path escapes workspace")
        if not path.is_file():
            raise LookupError("attachment file not found")
        return path

    def remove_file(self, workspace: Path) -> None:
        path = workspace / self.storage_path
        if path.is_file():
            path.unlink()
