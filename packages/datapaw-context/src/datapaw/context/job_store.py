"""Process-safe SQLite job and short-lived plan storage.

The store deliberately keeps orchestration state separate from API modules so
multiple workers share one source of truth.  It supports durable state,
idempotency keys, bounded retries, worker leases, heartbeat renewal, and
recovery of abandoned ``running`` jobs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import jobs_db_path


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "consumed"})
VALID_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "ready", "consumed"}
)


@dataclass(frozen=True)
class JobRecord:
    namespace: str
    job_id: str
    status: str
    payload: dict[str, Any]
    created_at: float
    updated_at: float
    heartbeat_at: float | None
    lease_owner: str | None
    lease_expires_at: float | None
    attempt: int
    max_attempts: int
    idempotency_key: str | None
    expires_at: float | None
    error: str | None


class PersistentJobStore:
    """Small SQLite-backed job store suitable for local multi-worker use."""

    def __init__(self, path: Path | None = None) -> None:
        configured = (os.getenv("DATAPAW_JOBS_DB") or "").strip()
        self.path = Path(configured).expanduser() if configured else (path or jobs_db_path())
        self.path = self.path.resolve()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    namespace TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    heartbeat_at REAL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    idempotency_key TEXT,
                    expires_at REAL,
                    error TEXT,
                    PRIMARY KEY (namespace, job_id),
                    UNIQUE (namespace, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON jobs(namespace, status, lease_expires_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_expiry
                    ON jobs(expires_at);
                """
            )

    @staticmethod
    def _payload_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _record(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            namespace=row["namespace"],
            job_id=row["job_id"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            heartbeat_at=row["heartbeat_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            idempotency_key=row["idempotency_key"],
            expires_at=row["expires_at"],
            error=row["error"],
        )

    def put(
        self,
        namespace: str,
        job_id: str,
        *,
        status: str,
        payload: dict[str, Any],
        ttl_seconds: float | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 1,
    ) -> JobRecord:
        if status not in VALID_STATUSES:
            raise ValueError(f"unsupported job status: {status}")
        if idempotency_key:
            existing = self.find_by_idempotency_key(namespace, idempotency_key)
            if existing is not None and existing.job_id != job_id:
                return existing
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                    namespace, job_id, status, payload_json, created_at,
                    updated_at, attempt, max_attempts, idempotency_key,
                    expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(namespace, job_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    max_attempts = excluded.max_attempts,
                    idempotency_key = COALESCE(
                        excluded.idempotency_key, jobs.idempotency_key
                    ),
                    expires_at = excluded.expires_at,
                    error = NULL
                    """,
                    (
                        namespace,
                        job_id,
                        status,
                        self._payload_json(payload),
                        now,
                        now,
                        max(1, max_attempts),
                        idempotency_key,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                connection.rollback()
                existing = self.find_by_idempotency_key(namespace, idempotency_key)
                if existing is None:
                    raise
                return existing
        # Read back without TTL filtering: a short-lived job may already be
        # past its expiry by the time the verification read runs.
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?",
                (namespace, job_id),
            ).fetchone()
        record = self._record(row)
        if record is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError(f"failed to persist job {namespace}/{job_id}")
        return record

    def get(self, namespace: str, job_id: str) -> JobRecord | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?",
                (namespace, job_id),
            ).fetchone()
            if row is not None and row["expires_at"] is not None and row["expires_at"] <= now:
                connection.execute(
                    "DELETE FROM jobs WHERE namespace = ? AND job_id = ?",
                    (namespace, job_id),
                )
                return None
        return self._record(row)

    def find_by_idempotency_key(
        self,
        namespace: str,
        idempotency_key: str,
    ) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE namespace = ? AND idempotency_key = ?",
                (namespace, idempotency_key),
            ).fetchone()
        record = self._record(row)
        if record and record.expires_at is not None and record.expires_at <= time.time():
            self.delete(namespace, record.job_id)
            return None
        return record

    def list(self, namespace: str, *, limit: int = 100) -> list[JobRecord]:
        self.delete_expired()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE namespace = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (namespace, max(1, min(limit, 1000))),
            ).fetchall()
        return [record for row in rows if (record := self._record(row)) is not None]

    def delete(self, namespace: str, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE namespace = ? AND job_id = ?",
                (namespace, job_id),
            )

    def delete_expired(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),),
            )
        return int(cursor.rowcount)

    def transition(
        self,
        namespace: str,
        job_id: str,
        *,
        expected: Iterable[str],
        status: str,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"unsupported job status: {status}")
        expected_values = tuple(expected)
        if not expected_values:
            raise ValueError("expected statuses must not be empty")
        placeholders = ",".join("?" for _ in expected_values)
        fields = ["status = ?", "updated_at = ?", "error = ?"]
        values: list[Any] = [status, time.time(), error]
        if payload is not None:
            fields.append("payload_json = ?")
            values.append(self._payload_json(payload))
        if status != "running":
            fields.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
        values.extend([namespace, job_id, *expected_values])
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET {', '.join(fields)}
                WHERE namespace = ? AND job_id = ?
                  AND status IN ({placeholders})
                """,
                values,
            )
        return cursor.rowcount == 1

    def claim(
        self,
        namespace: str,
        *,
        worker_id: str,
        lease_seconds: float = 60.0,
    ) -> JobRecord | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE namespace = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (
                    status = 'queued'
                    OR (status = 'running' AND lease_expires_at <= ?)
                  )
                  AND attempt < max_attempts
                ORDER BY created_at ASC LIMIT 1
                """,
                (namespace, now, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', updated_at = ?,
                    heartbeat_at = ?, lease_owner = ?, lease_expires_at = ?,
                    attempt = attempt + 1
                WHERE namespace = ? AND job_id = ?
                """,
                (
                    now,
                    now,
                    worker_id,
                    now + max(0.001, lease_seconds),
                    namespace,
                    row["job_id"],
                ),
            )
            connection.commit()
        return self.get(namespace, row["job_id"])

    def heartbeat(
        self,
        namespace: str,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE namespace = ? AND job_id = ? AND status = 'running'
                  AND lease_owner = ?
                """,
                (
                    now,
                    now + max(0.001, lease_seconds),
                    now,
                    namespace,
                    job_id,
                    worker_id,
                ),
            )
        return cursor.rowcount == 1

    def recover_stale(self, namespace: str) -> tuple[int, int]:
        """Requeue retryable expired leases and fail exhausted jobs."""
        now = time.time()
        with self._connect() as connection:
            requeued = connection.execute(
                """
                UPDATE jobs SET status = 'queued', updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE namespace = ? AND status = 'running'
                  AND lease_expires_at <= ? AND attempt < max_attempts
                """,
                (now, namespace, now),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE jobs SET status = 'failed', updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    error = COALESCE(error, 'worker lease expired')
                WHERE namespace = ? AND status = 'running'
                  AND lease_expires_at <= ? AND attempt >= max_attempts
                """,
                (now, namespace, now),
            ).rowcount
        return int(requeued), int(failed)

    def recover_interrupted(self) -> int:
        """Mark unleased in-process work as failed after a service restart."""
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'failed', updated_at = ?,
                    error = COALESCE(error, 'service restarted during execution')
                WHERE status = 'running' AND lease_expires_at IS NULL
                """,
                (now,),
            )
        return int(cursor.rowcount)


_store: PersistentJobStore | None = None


def get_job_store() -> PersistentJobStore:
    global _store
    if _store is None:
        _store = PersistentJobStore()
    return _store
