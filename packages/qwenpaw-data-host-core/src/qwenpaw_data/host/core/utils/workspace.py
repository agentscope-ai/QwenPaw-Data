# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

_SKIPPED_DIRS = {"skills", "__pycache__"}

# Windows refuses os.replace while another process holds the target open
# (antivirus, an editor, a concurrent reader); retry briefly instead of
# failing the write (#102).
_REPLACE_RETRIES = 10
_REPLACE_RETRY_DELAY_SECONDS = 0.05


def model_visible_path(value: str | PurePath) -> PurePath:
    """Render an executor path the way the model must see it.

    Sandbox paths stay POSIX regardless of the host OS: ``Path('/workspace')``
    on Windows is ``\\workspace`` but still names the Linux sandbox. Only
    drive-qualified/UNC paths (native Windows workspaces) stay Windows paths.
    Host filesystem operations must keep using concrete ``Path`` objects.
    """
    if isinstance(value, PurePosixPath):
        return value
    windows = PureWindowsPath(value)
    if windows.drive:
        return windows
    return PurePosixPath(str(value).replace("\\", "/"))


def write_json_atomic(path: Path, payload: Any, *, os_name: str = os.name) -> None:
    """Write JSON via a fsynced sibling temp file and ``os.replace``.

    Readers never observe a truncated document; the temp file is removed on
    any failure. On Windows a sharing violation on replace is retried.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, path, os_name=os_name)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _replace_with_retry(src: str, dst: Path, *, os_name: str) -> None:
    attempts = _REPLACE_RETRIES if os_name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


def list_session_files(workspace_dir: str | Path) -> list[dict[str, Any]]:
    """List workspace files for segment artifacts (rel_path relative to root)."""
    root = Path(workspace_dir)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if any(p.startswith(".") for p in parts):
            continue
        if parts[0] in _SKIPPED_DIRS:
            continue
        stat = path.stat()
        items.append(
            {
                "rel_path": rel.as_posix(),
                "size_bytes": stat.st_size,
                "mime_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return items
