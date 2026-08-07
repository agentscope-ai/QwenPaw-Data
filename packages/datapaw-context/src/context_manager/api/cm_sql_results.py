"""Bounded SQL-result cache and download-file lifecycle management."""

from __future__ import annotations

import csv
import io
import itertools
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datapaw.context.errors import ResourceBudgetExceeded
from datapaw.context.resource_budget import get_resource_limits

from .executor import ExecResult


SQL_PREVIEW_ROWS = 20
SQL_DOWNLOAD_MAX_ROWS = 10_000
SQL_DOWNLOAD_TTL_SECONDS = 24 * 3600
CM_LOCAL_BASE_URL = "http://127.0.0.1:8765"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def sql_downloads_dir() -> Path:
    return _PROJECT_ROOT / "data" / "sql_downloads"


@dataclass
class SqlCacheEntry:
    key: str
    result: ExecResult
    exec_status: str
    download_url: str | None
    file_path: Path
    created_at: float
    ttl: float

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl

    def touch(self) -> None:
        self.created_at = time.time()

    def expires_in_seconds(self) -> int:
        return max(0, int(self.created_at + self.ttl - time.time()))


class SqlResultCache:
    """Process-local lookup cache backed by bounded, expiring CSV files."""

    def __init__(self) -> None:
        self._store: dict[str, SqlCacheEntry] = {}

    @staticmethod
    def _make_key(sql: str, max_rows: int) -> str:
        return f"{sql.strip()}||max_rows={max_rows}"

    def get(self, sql: str, max_rows: int) -> SqlCacheEntry | None:
        key = self._make_key(sql, max_rows)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired() or not entry.file_path.exists():
            self._store.pop(key, None)
            return None
        return entry

    def put(
        self,
        sql: str,
        max_rows: int,
        result: ExecResult,
        exec_status: str,
        download_url: str | None,
        file_path: Path,
        ttl: float = SQL_DOWNLOAD_TTL_SECONDS,
    ) -> SqlCacheEntry:
        key = self._make_key(sql, max_rows)
        entry = SqlCacheEntry(
            key=key,
            result=result,
            exec_status=exec_status,
            download_url=download_url,
            file_path=file_path,
            created_at=time.time(),
            ttl=ttl,
        )
        self._store[key] = entry
        return entry

    def clear(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    def cleanup_expired(self) -> int:
        expired = [key for key, value in self._store.items() if value.is_expired()]
        for key in expired:
            self._store.pop(key, None)
        return len(expired)


sql_cache = SqlResultCache()


def save_sql_results_to_csv(
    columns: list[str],
    rows: list[list[Any]],
) -> tuple[str, Path]:
    """Persist a result without ever exceeding the configured response budget."""
    downloads_dir = sql_downloads_dir()
    downloads_dir.mkdir(parents=True, exist_ok=True)

    download_id = uuid.uuid4().hex[:8]
    file_path = downloads_dir / f"{download_id}.csv"
    max_bytes = get_resource_limits().max_response_bytes
    written = 0
    try:
        with file_path.open("w", newline="", encoding="utf-8") as handle:
            for row in itertools.chain([columns], rows):
                buffer = io.StringIO(newline="")
                csv.writer(buffer).writerow(row)
                text = buffer.getvalue()
                written += len(text.encode("utf-8"))
                if written > max_bytes:
                    raise ResourceBudgetExceeded(
                        "sql_download_bytes",
                        limit=max_bytes,
                        requested=written,
                    )
                handle.write(text)
    except BaseException:
        file_path.unlink(missing_ok=True)
        raise
    return download_id, file_path


def cleanup_expired_downloads() -> None:
    """Remove expired download files and their stale lookup entries."""
    sql_cache.cleanup_expired()
    downloads_dir = sql_downloads_dir()
    if not downloads_dir.exists():
        return

    cutoff = time.time() - SQL_DOWNLOAD_TTL_SECONDS
    for file_path in downloads_dir.glob("*.csv"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            # A concurrent cleanup/download may have already removed the file.
            continue
