"""Publish short-lived SQL CSV files in the authenticated workspace."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from uuid import uuid4

from ..auth import ScopeBinding
from ..settings import get_settings

_DIRECTORY_TOKEN = re.compile(r"[^A-Za-z0-9_.-]")
_RESULT_ID = re.compile(r"[A-Za-z0-9_-]+")


def _directory(scope: ScopeBinding) -> Path:
    """Resolve the workspace CSV directory using filesystem-safe scope names."""
    directory = get_settings().paths.home
    for part in (scope.tenant_id, scope.workspace_id, ".cm", "sql_downloads"):
        cleaned = _DIRECTORY_TOKEN.sub("_", (part or "").strip())[:64]
        directory /= "default" if not cleaned or cleaned in {".", ".."} else cleaned
        if directory.is_symlink():
            raise ValueError(f"symlinks are not allowed: {directory}")
    return directory


def cleanup_sql_results(*, scope: ScopeBinding) -> None:
    """Remove expired CSV files from the authorized Workspace directory."""
    directory = _directory(scope)
    cutoff = time() - get_settings().sql.retention_seconds
    for path in directory.glob("*.csv"):
        if _RESULT_ID.fullmatch(path.stem) is None:
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def save_sql_result(
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    scope: ScopeBinding,
) -> str:
    """Save JSON-safe query rows as UTF-8 CSV and return the published file id."""
    directory = _directory(scope)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    result_id = uuid4().hex
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=directory,
        prefix=".result-",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(
                    [
                        json.dumps(value, ensure_ascii=False, allow_nan=False)
                        if isinstance(value, (dict, list, tuple, bool))
                        else value
                        for value in row
                    ]
                )
            stream.close()
            temporary.replace(directory / f"{result_id}.csv")
        finally:
            temporary.unlink(missing_ok=True)
    return result_id


def sql_result_path(result_id: str, *, scope: ScopeBinding) -> Path:
    """Resolve an unexpired CSV in the authenticated workspace."""
    if _RESULT_ID.fullmatch(result_id) is None:
        raise FileNotFoundError("SQL result not found")
    path = _directory(scope) / f"{result_id}.csv"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("SQL result not found")
    if path.stat().st_mtime <= time() - get_settings().sql.retention_seconds:
        raise FileNotFoundError("SQL result not found")
    return path
