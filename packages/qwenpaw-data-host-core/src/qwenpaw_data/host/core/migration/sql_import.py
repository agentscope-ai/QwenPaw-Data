"""Transactional SQL component for the future offline import coordinator.

NOT a user-facing importer. The coordinator must verify source/backup, retain a
private durable plan/file journal and publish files in before_commit. This module
does not copy, delete, verify or recover files, nor provision runtime keys. It is
deliberately not wired to a CLI apply command until those pieces are complete.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from itertools import groupby, islice
from pathlib import Path
import re
import sqlite3
from typing import Callable

from sqlalchemy import and_, create_engine, insert, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from qwenpaw_data.host.core.db.tables import Base, LegacyImportRunRow

from .credentials import CredentialRewriter
from .key_setup import check_database_credentials
from .legacy_json import MigrationError, _canonical
from .plan import MigrationPlan
from .target import (
    _check_schema,
    _read_only_target,
    _sqlite_signature,
    check_target_connection,
)


_RUN = LegacyImportRunRow.__table__
_TABLES = frozenset(
    {
        "sessions",
        "chats",
        "attachments",
        "settlement_cards",
        "feedbacks",
        "cron_jobs",
        "chat_events",
        "segments",
        "biz_events",
        "artifacts",
        "followups",
        "user_providers",
        "user_provider_models",
        "user_active_models",
        "user_runtime_settings",
    }
)


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MigrationError("Import timestamps must be timezone aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def plan_digest(plan: MigrationPlan) -> str:
    """Bind a receipt to the EXACT prepared plan, not a regenerated conversion.

    Replanning re-encrypts credentials and chooses new conversion timestamps.
    Recovery must retain the original private plan, never guess equivalence.
    """
    if plan.issues or any(row.table not in _TABLES for row in plan.rows):
        raise MigrationError("SQL import requires a complete supported conversion plan")
    document = {
        "version": 1,
        "source": plan.source.digest,
        "target_home": str(plan.target_home),
        "prepared_at": plan.prepared_at,
        "identities": [vars(identity) for identity in plan.identities],
        "rows": [{"table": row.table, "values": row.values} for row in plan.rows],
        "files": [vars(file) for file in plan.files],
        "directories": plan.directories,
    }
    try:
        return hashlib.sha256(_canonical(_json_value(document))).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise MigrationError(
            "Prepared import data cannot be canonically encoded"
        ) from None


def _run_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise MigrationError(
            "Import run ID must be 32 lowercase hexadecimal characters"
        )


def _data_digest(conn, plan: MigrationPlan) -> str:
    """Hash every native column of exactly the imported rows, in bounded batches."""
    groups = defaultdict(list)
    for row in plan.rows:
        groups[row.table].append(row)
    digest = hashlib.sha256()
    for name in sorted(groups):
        table = Base.metadata.tables[name]
        keys = tuple(table.primary_key.columns)
        records = groups[name]
        batch = max(1, 400 // len(keys))
        for offset in range(0, len(records), batch):
            selected = records[offset : offset + batch]
            conditions = [
                and_(*(key == row.values[key.name] for key in keys)) for row in selected
            ]
            found = {
                tuple(value[key.name] for key in keys): dict(value)
                for value in conn.execute(
                    select(table).where(or_(*conditions))
                ).mappings()
            }
            for row in selected:
                value = found.get(tuple(row.values[key.name] for key in keys))
                if value is None:
                    raise MigrationError(
                        "An imported SQL row is missing; automatic replay is refused"
                    )
                encoded = _canonical(_json_value({"table": name, "row": value}))
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class SQLReceipt:
    run_id: str
    plan_digest: str
    data_digest: str
    row_count: int
    file_count: int
    status: str

    def report(self) -> dict:
        return {
            **vars(self),
            "stage": "sql_import_component",
            "sql_written": self.status == "committed",
            "files_verified": False,
            "warning": "SQL receipt only; the offline coordinator must separately verify file publication and recovery journal. Not full migration acceptance.",
        }


def _receipt(conn, plan, run_id, fingerprint):
    value = (
        conn.execute(select(_RUN).where(_RUN.c.id == run_id)).mappings().one_or_none()
    )
    if value is None:
        return None
    if (
        value["version"] != 1
        or value["source_digest"] != plan.source.digest
        or value["plan_digest"] != fingerprint
        or value["row_count"] != len(plan.rows)
        or value["file_count"] != len(plan.files)
    ):
        raise MigrationError(
            "Import run ID belongs to a different plan; no replay or overwrite is allowed"
        )
    if _data_digest(conn, plan) != value["data_digest"]:
        raise MigrationError(
            "Imported SQL rows have changed; automatic replay is refused"
        )
    return SQLReceipt(
        run_id,
        fingerprint,
        value["data_digest"],
        value["row_count"],
        value["file_count"],
        "already_committed",
    )


@contextmanager
def _writer(database_url: str, *, recover_sqlite: bool = False):
    engine = None
    try:
        if (
            not isinstance(database_url, str)
            or not database_url
            or len(database_url) > 16384
        ):
            raise MigrationError("An explicit bounded target database URL is required")
        url = make_url(database_url)
        if url.drivername in {"sqlite", "sqlite+pysqlite"}:
            if (
                url.query
                or url.host
                or url.username
                or url.password
                or not url.database
                or not Path(url.database).is_absolute()
            ):
                raise MigrationError(
                    "Target SQLite URL requires an absolute file path without query options"
                )
            path = Path(url.database)
            _sqlite_signature(path, recover_journals=recover_sqlite)
            path = path.resolve(strict=True)
            engine = create_engine(
                "sqlite+pysqlite://",
                creator=lambda: sqlite3.connect(
                    path.as_uri() + "?mode=rw", uri=True, timeout=5
                ),
                poolclass=NullPool,
                hide_parameters=True,
            )
        elif url.drivername in {"postgresql", "postgresql+psycopg"}:
            if not url.database or not url.host or not url.username:
                raise MigrationError(
                    "Target PostgreSQL URL requires an explicit host, user and database"
                )
            engine = create_engine(
                url.set(drivername="postgresql+psycopg"),
                poolclass=NullPool,
                hide_parameters=True,
                connect_args={"connect_timeout": 10},
            )
        else:
            raise MigrationError(
                "SQL import supports file SQLite or synchronous PostgreSQL only"
            )
        with engine.connect() as conn:
            try:
                if conn.dialect.name == "sqlite":
                    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    conn.exec_driver_sql("SET LOCAL statement_timeout=10000")
                    conn.exec_driver_sql("SET LOCAL lock_timeout=1000")
                    # One migration writer per database, even for unrelated run IDs.
                    conn.exec_driver_sql("SELECT pg_advisory_xact_lock(1364214857)")
                yield conn
            finally:
                conn.rollback()  # A successful explicit commit makes this a no-op.
    except (SQLAlchemyError, OSError, ValueError, ImportError) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "SQL import failed; verify the durable run receipt before any file cleanup or retry"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


def read_sql_receipt(
    plan: MigrationPlan, *, database_url: str, run_id: str, target_stopped: bool = False
) -> SQLReceipt | None:
    """None means absent in a successful read, never a connection/schema failure."""
    if target_stopped is not True:
        raise MigrationError("Receipt inspection requires stopped target writers")
    _run_id(run_id)
    fingerprint = plan_digest(plan)
    with _read_only_target(database_url) as conn:
        _check_schema(conn, {row.table for row in plan.rows} | {_RUN.name})
        return _receipt(conn, plan, run_id, fingerprint)


def commit_sql_rows(
    plan: MigrationPlan,
    *,
    database_url: str,
    run_id: str,
    target_key: bytes,
    before_commit: Callable[[], None],
    target_stopped: bool = False,
    recover_sqlite: bool = False,
) -> SQLReceipt:
    """Insert rows + receipt atomically around the coordinator's file hook.

    before_commit MUST durably publish/verify files and its private recovery
    journal. It must not access this SQL transaction, commit it, or start target
    services. On any failure, the coordinator must inspect the durable receipt
    before deciding whether cleanup is safe. No automatic file cleanup occurs.
    Only a coordinator with a verified owned recovery journal may opt into
    recover_sqlite; SQLite itself handles its journals, never manual deletion.
    """
    if target_stopped is not True or not callable(before_commit):
        raise MigrationError(
            "SQL import requires stopped target writers and an explicit file-coordination hook"
        )
    _run_id(run_id)
    plan = deepcopy(plan)  # Caller mutations cannot change the committed manifest.
    fingerprint = plan_digest(plan)
    verifier = CredentialRewriter(source_key=target_key, target_key=target_key)
    for row in plan.rows:
        if row.table == "user_providers":
            value = row.values.get("api_key_enc")
            if not isinstance(value, str) or not value.startswith("ENC:"):
                raise MigrationError(
                    "Prepared provider credentials must be encrypted under the explicit target key"
                )
            verifier.rewrite(
                value
            )  # Authenticate, but preserve the prepared ciphertext.
    if type(recover_sqlite) is not bool:
        raise MigrationError("SQLite recovery must be an explicit boolean")
    with _writer(database_url, recover_sqlite=recover_sqlite) as conn:
        _check_schema(conn, {row.table for row in plan.rows} | {_RUN.name})
        existing = _receipt(conn, plan, run_id, fingerprint)
        checks = check_target_connection(plan, conn, check_conflicts=existing is None)
        if checks.issues:
            raise MigrationError(
                "Target authorization or primary-key checks failed inside the import transaction"
            )
        check_database_credentials(conn, target_key)
        if existing:
            return existing
        for (name, _), records in groupby(
            plan.rows, key=lambda row: (row.table, tuple(sorted(row.values)))
        ):
            while batch := list(islice(records, 200)):
                conn.execute(
                    insert(Base.metadata.tables[name]), [row.values for row in batch]
                )
        data_digest = _data_digest(conn, plan)
        try:
            before_commit()
        except Exception:
            raise MigrationError(
                "File-coordination hook failed; SQL transaction was not committed; retain the recovery journal"
            ) from None
        conn.execute(
            insert(_RUN),
            dict(
                id=run_id,
                version=1,
                source_digest=plan.source.digest,
                plan_digest=fingerprint,
                data_digest=data_digest,
                row_count=len(plan.rows),
                file_count=len(plan.files),
                committed_at=datetime.now(timezone.utc),
            ),
        )
        # A connection loss here is ambiguous. Never infer rollback from the
        # client exception: the durable receipt is the only completion evidence.
        conn.commit()
    return SQLReceipt(
        run_id, fingerprint, data_digest, len(plan.rows), len(plan.files), "committed"
    )


def recover_sql_receipt(
    plan: MigrationPlan, *, database_url: str, run_id: str, target_stopped: bool = False
) -> SQLReceipt | None:
    """Explicit engine-level crash recovery for the future journal coordinator.

    Unlike read_sql_receipt this opens a writable connection and SQLite may
    recover/checkpoint its own journals. No migration rows or markers are
    inserted/deleted. A coordinator must hold its file-journal lock and verify
    target ownership first. Cold SQLite sidecars can remain until a retry with
    recover_sqlite=True; never remove them manually or interpret errors as None.
    """
    if target_stopped is not True:
        raise MigrationError("SQL recovery requires stopped target writers")
    _run_id(run_id)
    fingerprint = plan_digest(plan)
    with _writer(database_url, recover_sqlite=True) as conn:
        _check_schema(conn, {row.table for row in plan.rows} | {_RUN.name})
        return _receipt(conn, plan, run_id, fingerprint)
