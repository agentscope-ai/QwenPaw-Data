"""Explicit read-only target checks; never bootstrap Auth, schema, or keys.

This is a point-in-time preflight, not an import permit or a reservation. The
eventual importer must recheck under its own concurrency/rollback protocol.
SQLite requires stopped writers and no journal sidecars: immutable read-only
mode avoids even creating WAL/SHM files. PostgreSQL uses a read-only repeatable
read transaction. PostgreSQL-compatible services must support that protocol;
no Hologres acceptance is implied.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat

from sqlalchemy import and_, create_engine, func, inspect, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from qwenpaw_data_auth.database import metadata as auth_metadata
from qwenpaw_data.host.core.db.tables import Base

from .legacy_json import MigrationError, _signature
from .plan import MigrationPlan, PlanIssue


@dataclass(frozen=True)
class TargetCheck:
    backend: str
    identities_checked: int
    rows_checked: int
    issues: tuple[PlanIssue, ...]

    def report(self) -> dict:
        return {
            "stage": "read_only_target_check",
            "backend": self.backend,
            "target_database_checked": True,
            "target_identity_membership_checked": True,
            "target_checks_passed": not self.issues,
            "identities_checked": self.identities_checked,
            "rows_checked": self.rows_checked,
            "issues": [vars(issue) for issue in self.issues],
            "target_master_key_provisioned": False,
            "import_ready": False,
            "sql_written": False,
            "target_files_written": False,
            "warning": "Point-in-time target preflight only. No accounts, schema or keys are created. Recheck identities, schema, conflicts and files during transactional import; no target reservation is held.",
        }


def _sqlite_signature(path: Path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError(
            "Target SQLite must be an existing regular non-symlink file"
        )
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MigrationError("Target SQLite must be a regular file without hard links")
    if any(
        Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise MigrationError(
            "Target SQLite has journal sidecars; stop writers and checkpoint using its owning application first"
        )
    return _signature(info)


@contextmanager
def _read_only_target(database_url: str):
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
            selected = Path(url.database)
            before = _sqlite_signature(selected)
            path = selected.resolve(strict=True)
            engine = create_engine(
                "sqlite+pysqlite://",
                creator=lambda: sqlite3.connect(
                    path.as_uri() + "?mode=ro&immutable=1", uri=True
                ),
                poolclass=NullPool,
                hide_parameters=True,
            )
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA query_only=ON")
                conn.exec_driver_sql("BEGIN")
                try:
                    yield conn
                finally:
                    conn.rollback()
            if (
                selected.resolve(strict=True) != path
                or _sqlite_signature(selected) != before
            ):
                raise MigrationError("Target SQLite changed during validation")
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
            with engine.connect() as conn:
                try:
                    conn.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    conn.exec_driver_sql("SET LOCAL statement_timeout = 10000")
                    conn.exec_driver_sql("SET LOCAL lock_timeout = 1000")
                    yield conn
                finally:
                    conn.rollback()
        else:
            raise MigrationError(
                "Target validation supports file SQLite or synchronous PostgreSQL (psycopg) only"
            )
    except (SQLAlchemyError, OSError, ValueError, ImportError) as exc:
        if isinstance(exc, MigrationError):
            raise
        # Driver errors can contain DSNs, passwords, bound IDs, or row values.
        raise MigrationError(
            "Target database validation failed; verify connectivity, driver and current schema without sharing credentials"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


_AUTH_COLUMNS = {
    "users": {"id", "tenant_id", "status"},
    "tenants": {"id", "status"},
    "workspaces": {"id", "tenant_id", "status"},
    "role_bindings": {"user_id", "role", "scope_type", "scope_id"},
}


def _check_schema(conn, tables):
    inspector = inspect(conn)
    present = set(inspector.get_table_names())  # A view is not an import target.
    required = {
        **_AUTH_COLUMNS,
        **{name: set(Base.metadata.tables[name].columns.keys()) for name in tables},
    }
    for name, columns in required.items():
        if name not in present or not columns <= {
            c["name"] for c in inspector.get_columns(name)
        }:
            raise MigrationError(
                f"Target schema is missing required table/columns: {name}; initialize or upgrade it separately"
            )
        if name in tables:
            actual = set(inspector.get_pk_constraint(name)["constrained_columns"])
            expected = {c.name for c in Base.metadata.tables[name].primary_key.columns}
            if actual != expected:
                raise MigrationError(
                    f"Target primary key does not match the native schema: {name}"
                )


def _authorized(conn, identity):
    users, tenants, workspaces, bindings = (
        auth_metadata.tables[name]
        for name in ("users", "tenants", "workspaces", "role_bindings")
    )
    # Match current Auth authorization, including tenant-admin access to an
    # active workspace in their own tenant. Never infer roles from legacy attrs.
    conditions = (
        (
            users,
            and_(
                users.c.id == identity.user_id,
                users.c.tenant_id == identity.tenant_id,
                users.c.status == "active",
            ),
        ),
        (
            tenants,
            and_(tenants.c.id == identity.tenant_id, tenants.c.status == "active"),
        ),
        (
            workspaces,
            and_(
                workspaces.c.id == identity.workspace_id,
                workspaces.c.tenant_id == identity.tenant_id,
                workspaces.c.status == "active",
            ),
        ),
        (
            bindings,
            and_(
                bindings.c.user_id == identity.user_id,
                or_(
                    and_(
                        bindings.c.scope_type == "tenant",
                        bindings.c.scope_id == identity.tenant_id,
                        bindings.c.role == "tenant_admin",
                    ),
                    and_(
                        bindings.c.scope_type == "workspace",
                        bindings.c.scope_id == identity.workspace_id,
                        bindings.c.role.in_(("member", "ws_admin")),
                    ),
                ),
            ),
        ),
    )
    return all(
        conn.scalar(select(select(1).select_from(table).where(condition).exists()))
        for table, condition in conditions
    )


def validate_target(
    plan: MigrationPlan, *, database_url: str, target_stopped: bool = False
) -> TargetCheck:
    """Check only an explicitly selected, already initialized target database."""
    if target_stopped is not True:
        raise MigrationError(
            "Target validation requires explicit confirmation that target writers are stopped"
        )
    if plan.issues:
        raise MigrationError(
            "Resolve all source conversion issues before target validation"
        )
    identities = {(i.user_id, i.tenant_id, i.workspace_id) for i in plan.identities}
    user_ids = {i.user_id for i in plan.identities}
    rows = defaultdict(list)
    for row in plan.rows:
        if (
            row.table not in Base.metadata.tables
            or row.values.get("user_id") not in user_ids
        ):
            raise MigrationError(
                "Prepared rows require their original explicit target identities"
            )
        if (
            "tenant_id" in row.values
            and tuple(
                row.values.get(k) for k in ("user_id", "tenant_id", "workspace_id")
            )
            not in identities
        ):
            raise MigrationError(
                "Prepared row scope differs from its explicit target identity"
            )
        rows[row.table].append(row)
    issues = []
    with _read_only_target(database_url) as conn:
        _check_schema(conn, rows)
        for identity in plan.identities:
            if not _authorized(conn, identity):
                issues.append(
                    PlanIssue(
                        "target_identity_not_authorized",
                        f"identities/{identity.user_id}",
                    )
                )
        conflicts = Counter()
        for name, records in rows.items():
            table = Base.metadata.tables[name]
            keys = tuple(table.primary_key.columns)
            # Bounded parameter counts even for composite keys on older SQLite.
            batch_size = max(1, 400 // len(keys))
            for offset in range(0, len(records), batch_size):
                conditions = [
                    and_(*(key == row.values[key.name] for key in keys))
                    for row in records[offset : offset + batch_size]
                ]
                conflicts[name] += conn.scalar(
                    select(func.count()).select_from(table).where(or_(*conditions))
                )
        issues.extend(
            PlanIssue(
                "target_primary_key_conflict",
                name,
                f"{count} existing rows; no overwrite or implicit merge",
            )
            for name, count in conflicts.items()
            if count
        )
        backend = conn.dialect.name
    return TargetCheck(
        backend, len(plan.identities), len(plan.rows), tuple(sorted(issues))
    )
