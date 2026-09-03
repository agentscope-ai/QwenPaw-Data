# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.time import utcnow

MAX_SIZE_BYTES = 20 * 1024 * 1024


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
        name = require_safe_name(filename)
        if not data:
            raise ValueError("file is empty")
        if len(data) > MAX_SIZE_BYTES:
            raise ValueError("file exceeds 20MB")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists():
            raise ValueError(f"duplicate filename: {name}")
        dest.write_bytes(data)
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
