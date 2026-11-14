"""Auth control-plane database schema definitions and helpers."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import expression

from qwenpaw_data_auth.config import runtime_home

# ---------------------------------------------------------------------------
# Schema (SQLAlchemy Core Table metadata)
# ---------------------------------------------------------------------------

metadata = MetaData()


# ---------------------------------------------------------------------------
# Dialect-aware DDL helpers (SQLite / Hologres)
# ---------------------------------------------------------------------------
#
# The three schema-level dialect differences are all handled here so the Table
# definitions stay declarative:
#
#   1. ``created_at`` / ``timestamp`` default — SQLite has ``strftime`` but
#      Hologres does not; both must yield the *same* second-precision ISO-8601
#      string so API responses stay byte-identical. ``_TS_DEFAULT`` compiles to
#      ``strftime(...)`` on SQLite and ``to_char(now() ...)`` on Hologres.
#   2. Foreign keys — Hologres (PostgreSQL wire protocol) does not support FK;
#      each ``ForeignKeyConstraint`` is wrapped in ``.ddl_if(dialect="sqlite")``
#      so it is emitted only on SQLite. Referential integrity is enforced in the
#      application layer on Hologres (consistent with the other modules).
#   3. UNIQUE — Hologres supports no UNIQUE constraints/indexes; every
#      ``UniqueConstraint`` is likewise SQLite-only. Uniqueness is guaranteed by
#      existing application-layer checks (login/token de-dup) on Hologres.
#
# Hologres control-plane tables are point read/write, so they are created
# row-oriented (``postgresql_with``); the kwarg is ignored by SQLite.


class _utcnow_iso(expression.FunctionElement):
    """DEFAULT producing a second-precision ISO-8601 UTC timestamp, per dialect."""

    type = Text()
    inherit_cache = True


@compiles(_utcnow_iso, "sqlite")
def _utcnow_iso_sqlite(element, compiler, **kw):  # noqa: ANN001, ARG001
    return "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"


@compiles(_utcnow_iso)
def _utcnow_iso_default(element, compiler, **kw):  # noqa: ANN001, ARG001
    # Hologres speaks the PostgreSQL dialect; ``to_char`` mirrors the SQLite
    # ``strftime`` format exactly (``2026-08-13T03:37:05Z``).
    return "(to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))"


_TS_DEFAULT = _utcnow_iso()

# Row-oriented storage for Hologres point access; ignored by SQLite. The value
# is stashed in ``Table.info`` and materialised as a ``WITH (orientation='row')``
# clause by the PostgreSQL ``CreateTable`` hook below (SQLAlchemy has no generic
# per-table ``WITH`` kwarg). Scoping via ``info`` keeps the hook from touching
# unrelated tables created in the same process.
_HOLO_ROW = {"info": {"holo_orientation": "row"}}
_ddl_hologres: ContextVar[bool | None] = ContextVar("auth_ddl_hologres", default=None)


def _target_is_hologres() -> bool:
    """True 仅当控制面 DSN 指向 Hologres（其 host 带 ``hologres.aliyuncs.com``）。

    ``orientation`` 是 Hologres 特有存储参数；标准 PostgreSQL / PolarDB for PG
    不认，DDL 会报 ``unrecognized parameter "orientation"``。据 QWENPAW_DATA_DB_URL 的
    host 自动判定：只有 Hologres 才注入行存子句，真 PG 走默认编译。
    """
    selected = _ddl_hologres.get()
    if selected is not None:
        return selected
    return "hologres.aliyuncs.com" in os.environ.get("QWENPAW_DATA_DB_URL", "")


@compiles(CreateTable, "postgresql")
def _pg_create_table_with_orientation(element, compiler, **kw):  # noqa: ANN001, ARG001
    sql = compiler.visit_create_table(element, **kw)
    orientation = element.element.info.get("holo_orientation")
    if orientation and _target_is_hologres():
        # Append after the closing paren; ``visit_create_table`` ends with
        # ``)\n\n`` which we trim before adding the Hologres storage clause.
        sql = sql.rstrip() + f" WITH (orientation='{orientation}')\n\n"
    return sql


def _sqlite_fk(columns, refcolumns, name):  # noqa: ANN001, ANN201
    """Foreign key emitted only on SQLite (Hologres has no FK support)."""
    return ForeignKeyConstraint(columns, refcolumns, name=name).ddl_if(dialect="sqlite")


def _sqlite_unique(*columns, name):  # noqa: ANN002, ANN201
    """UNIQUE constraint emitted only on SQLite (Hologres has no UNIQUE)."""
    return UniqueConstraint(*columns, name=name).ddl_if(dialect="sqlite")


tenants = Table(
    "tenants",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("neo4j_database", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("onboarding_state", Text, nullable=False, server_default=text("'ready'")),
    Column("onboarding_error", Text),
    Column("join_open", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    **_HOLO_ROW,
)

workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("endpoint_ref", Text),
    Column("secret_ref", Text),
    Column("instance_status", Text, nullable=False, server_default=text("'pending'")),
    Column("instance_version", Text),
    Column("join_open", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    _sqlite_fk(["tenant_id"], ["tenants.id"], name="fk_workspaces_tenant"),
    **_HOLO_ROW,
)

users = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("username", Text, nullable=False),
    Column("password_hash", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    Column("token_revoked_before", Text),
    Column("failed_login_count", Integer, nullable=False, server_default=text("0")),
    Column("locked_until", Text),
    Column("password_changed_at", Text),
    Column("force_password_change", Integer, nullable=False, server_default=text("0")),
    Column("active_workspace_id", Text),
    # Optional human-readable name shown in the UI.
    Column("display_name", Text),
    _sqlite_fk(["tenant_id"], ["tenants.id"], name="fk_users_tenant"),
    _sqlite_unique("tenant_id", "username", name="uq_users_tenant_username"),
    **_HOLO_ROW,
)

role_bindings = Table(
    "role_bindings",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("scope_id", Text, nullable=False),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    _sqlite_fk(["user_id"], ["users.id"], name="fk_role_bindings_user"),
    **_HOLO_ROW,
)

access_requests = Table(
    "access_requests",
    metadata,
    Column("id", Text, primary_key=True),
    # The applicant is always a persisted local account.
    Column("user_id", Text, nullable=False),
    Column("name_snapshot", Text, nullable=False, server_default=text("''")),
    Column("target_type", Text, nullable=False),
    Column(
        "target_id", Text, nullable=False
    ),  # tenant id or workspace id depending on ``target_type``; polymorphic, so no FK.
    Column("tenant_id", Text, nullable=False),
    Column("requested_role", Text, nullable=False, server_default=text("'member'")),
    Column("message", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("reviewer_user_id", Text),
    Column("review_comment", Text, nullable=False, server_default=text("''")),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    Column("reviewed_at", Text),
    _sqlite_fk(["tenant_id"], ["tenants.id"], name="fk_access_requests_tenant"),
    _sqlite_fk(["user_id"], ["users.id"], name="fk_access_requests_user"),
    Index("idx_access_requests_review", "tenant_id", "status").ddl_if(dialect="sqlite"),
    Index("idx_access_requests_applicant", "user_id", "status").ddl_if(dialect="sqlite"),
    **_HOLO_ROW,
)

authorization_codes = Table(
    "authorization_codes",
    metadata,
    Column("code", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("redirect_uri", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Column("used", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    _sqlite_fk(["user_id"], ["users.id"], name="fk_authorization_codes_user"),
    **_HOLO_ROW,
)

refresh_tokens = Table(
    "refresh_tokens",
    metadata,
    Column("jti", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    # Unix epoch seconds. ``BigInteger`` (int8) not ``Integer`` — on Hologres
    # ``Integer`` is 32-bit int4 and would overflow past 2038; SQLite treats
    # both as 64-bit so this is transparent there.
    Column("expires_at", BigInteger, nullable=False),
    Column("consumed_at", Text),
    Column("replaced_by", Text),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    _sqlite_fk(["user_id"], ["users.id"], name="fk_refresh_tokens_user"),
    **_HOLO_ROW,
)

platform_admins = Table(
    "platform_admins",
    metadata,
    Column("id", Text, primary_key=True),
    Column("username", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("token_revoked_before", Text),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    # Optional human-readable name shown in the platform console.
    Column("display_name", Text),
    _sqlite_unique("username", name="uq_platform_admins_username"),
    **_HOLO_ROW,
)

raw_cypher_grants = Table(
    "raw_cypher_grants",
    metadata,
    Column("id", Text, primary_key=True),
    Column("token_hash", Text, nullable=False),
    Column("platform_admin_id", Text, nullable=False),
    Column("actor_user_id", Text, nullable=False),
    Column("tenant_id", Text, nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("query_digest", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Column("consumed_at", Text),
    Column("revoked_at", Text),
    Column("created_at", Text, nullable=False, server_default=_TS_DEFAULT),
    _sqlite_fk(["platform_admin_id"], ["platform_admins.id"], name="fk_grants_admin"),
    _sqlite_fk(["actor_user_id"], ["users.id"], name="fk_grants_actor"),
    _sqlite_fk(["tenant_id"], ["tenants.id"], name="fk_grants_tenant"),
    _sqlite_fk(["workspace_id"], ["workspaces.id"], name="fk_grants_workspace"),
    _sqlite_unique("token_hash", name="uq_raw_cypher_grants_token_hash"),
    # Secondary index is SQLite-only: Hologres has no ``CREATE INDEX`` (it uses
    # clustering/segment keys instead); scope lookups stay correct without it.
    Index(
        "idx_raw_cypher_grants_scope",
        "actor_user_id",
        "tenant_id",
        "workspace_id",
        "expires_at",
    ).ddl_if(dialect="sqlite"),
    **_HOLO_ROW,
)

audit_logs = Table(
    "audit_logs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("timestamp", Text, nullable=False, server_default=_TS_DEFAULT),
    Column("event_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("actor_ip", Text, nullable=False, server_default=text("''")),
    Column("target_type", Text, nullable=False, server_default=text("''")),
    Column("target_id", Text, nullable=False, server_default=text("''")),
    Column("detail", Text),
    **_HOLO_ROW,
)


# Idempotent, SQLite-only column back-fills (SQLite has no portable
# ``ADD COLUMN IF NOT EXISTS``). Databases created from the Table definitions
# already contain these columns.
_LEGACY_MIGRATIONS: dict[str, dict[str, str]] = {
    "tenants": {
        "onboarding_state": "TEXT NOT NULL DEFAULT 'ready'",
        "onboarding_error": "TEXT",
        "join_open": "INTEGER NOT NULL DEFAULT 0",
    },
    "workspaces": {
        "endpoint_ref": "TEXT",
        "secret_ref": "TEXT",
        "instance_status": "TEXT NOT NULL DEFAULT 'pending'",
        "instance_version": "TEXT",
        "join_open": "INTEGER NOT NULL DEFAULT 0",
    },
    "users": {
        "active_workspace_id": "TEXT",
        "display_name": "TEXT",
    },
    "platform_admins": {
        "display_name": "TEXT",
    },
}


# ---------------------------------------------------------------------------
# Engine / URL resolution
# ---------------------------------------------------------------------------

_engines: dict[str, Engine] = {}


def control_plane_dsn() -> str:
    """The canonical control-plane DSN.

    Every control-plane store (auth, host, context, semantic config) lives in a
    single unified database selected by ``QWENPAW_DATA_DB_URL``. Returns an empty
    string when unset.
    """
    return os.environ.get("QWENPAW_DATA_DB_URL", "").strip()


def shared_database_url(dsn: str = "") -> str:
    """Normalize the SQL URL shared by all services; blank selects the local default."""
    return _resolve_url(dsn.strip())[0]


def _runtime_path(raw: str) -> str:
    """Anchor a relative SQLite path at the runtime home, not the process cwd."""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else runtime_home() / path)


def _resolve_url(dsn: str = "") -> tuple[str, Optional[str]]:
    """Normalise a DSN into a **synchronous** SQLAlchemy URL.

    Returns ``(url, sqlite_path)`` where ``sqlite_path`` is the on-disk file for
    SQLite backends (used to create the parent directory) and ``None`` otherwise.
    Accepts the shared ``QWENPAW_DATA_DB_URL`` (possibly written with async drivers
    such as ``+aiosqlite``/``+asyncpg``) as well as bare-path / ``sqlite:///``
    forms.
    """
    raw = dsn or os.environ.get("QWENPAW_DATA_DB_URL") or ""
    if not raw:
        # Local state lives under QWENPAW_DATA_HOME next to the auth secrets.
        path = str(runtime_home() / "control_plane.db")
        return f"sqlite+pysqlite:///{path}", path

    if "://" not in raw:
        # Bare filesystem path → local SQLite file.
        path = _runtime_path(raw)
        return f"sqlite+pysqlite:///{path}", path

    url = make_url(raw)
    backend = url.get_backend_name()
    if backend == "sqlite":
        url = url.set(drivername="sqlite+pysqlite")
        if url.database and url.database != ":memory:":
            url = url.set(database=_runtime_path(url.database))
        return url.render_as_string(hide_password=False), url.database
    if backend == "postgresql":
        # Hologres speaks the PostgreSQL wire protocol; use the sync psycopg
        # driver regardless of how the shared URL spelled the async driver.
        url = url.set(drivername="postgresql+psycopg")
        return url.render_as_string(hide_password=False), None
    return raw, None


def _get_engine(dsn: str = "") -> Engine:
    url, sqlite_path = _resolve_url(dsn)
    engine = _engines.get(url)
    if engine is not None:
        return engine

    if sqlite_path is not None:
        options: dict[str, Any] = {}
        if sqlite_path in {"", ":memory:"}:
            # Every thread must see the same in-memory database.
            options["poolclass"] = StaticPool
        else:
            # The control plane holds password hashes and refresh tokens: create
            # a new file owner-only, but never chmod an existing shared database.
            os.makedirs(os.path.dirname(sqlite_path) or ".", mode=0o700, exist_ok=True)
            try:
                fd = os.open(sqlite_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            **options,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    else:
        engine = create_engine(url, future=True)

    _engines[url] = engine
    return engine


def reset_engines() -> None:
    """Dispose cached engines (tests may point at fresh temporary databases)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


class _Row:
    """Row that indexes by both column name and position, like ``sqlite3.Row``.

    SQLAlchemy ``Row`` only supports positional/attribute access; this wrapper
    also supports ``row["col"]`` so ``conn.execute(...).fetchone()`` consumers
    (CM, tests) can index by name or position.
    """

    __slots__ = ("_r",)

    def __init__(self, row) -> None:  # noqa: ANN001
        self._r = row

    def __getitem__(self, key):  # noqa: ANN001
        if isinstance(key, str):
            return self._r._mapping[key]
        return self._r[key]

    def __iter__(self):
        return iter(self._r)

    def keys(self):
        return list(self._r._mapping.keys())

    def get(self, key, default=None):  # noqa: ANN001
        return self._r._mapping.get(key, default)


class _Result:
    """Cursor-like view over a SQLAlchemy result (``fetchone`` / ``fetchall``)."""

    __slots__ = ("_res",)

    def __init__(self, res) -> None:  # noqa: ANN001
        self._res = res

    @property
    def rowcount(self) -> int:
        return self._res.rowcount

    @property
    def lastrowid(self):
        return self._res.lastrowid

    def fetchone(self):
        row = self._res.fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(row) for row in self._res.fetchall()]

    def mappings(self):
        return self._res.mappings()

    def __iter__(self):
        return (_Row(row) for row in self._res)


class _Conn:
    """Thin sync wrapper around a SQLAlchemy connection.

    ``execute`` accepts either a raw SQL string (used by CM; routed through the
    driver so SQLite ``?`` params work) **or** a SQLAlchemy Core statement.
    The returned ``_Result`` exposes ``rowcount`` / ``fetchone`` / ``fetchall`` /
    ``mappings``.
    """

    def __init__(self, sa_conn) -> None:  # noqa: ANN001
        self._c = sa_conn

    def _sql(self, statement: str) -> str:
        """Adapt a raw ``?``-placeholder string to the backend paramstyle.

        CM ``auth_middleware`` and other callers hand us raw SQL with SQLite's
        ``?`` placeholders. SQLite/pysqlite accept ``?`` (qmark), but Hologres
        via psycopg uses ``%s`` (pyformat); passing ``?`` there raises and
        surfaces as a 503. Rewrite ``?`` → ``%s`` for non-SQLite backends so
        the shared raw-SQL contract works on both dialects.
        """
        if self._c.dialect.name == "sqlite":
            return statement
        return statement.replace("?", "%s")

    def execute(self, statement, params: tuple = ()) -> _Result:  # noqa: ANN001
        if isinstance(statement, str):
            res = self._c.exec_driver_sql(
                self._sql(statement), tuple(params) if params else ()
            )
        else:
            res = self._c.execute(statement)
        return _Result(res)

    def executemany(self, statement, seq_of_params) -> _Result:  # noqa: ANN001
        rows = [tuple(p) for p in seq_of_params]
        if isinstance(statement, str):
            res = self._c.exec_driver_sql(self._sql(statement), rows)
        else:
            res = self._c.execute(statement, rows)
        return _Result(res)

    def commit(self) -> None:
        self._c.commit()

    def rollback(self) -> None:
        self._c.rollback()

    def close(self) -> None:
        self._c.close()


@contextmanager
def get_db(dsn: str = "") -> Generator[_Conn, None, None]:
    engine = _get_engine(dsn)
    raw = engine.connect()
    try:
        yield _Conn(raw)
    finally:
        raw.close()


_HOLO_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tenants", "join_open", "INTEGER"),
    ("workspaces", "join_open", "INTEGER"),
    ("users", "display_name", "TEXT"),
    ("platform_admins", "display_name", "TEXT"),
)


def _migrate_columns(conn, sqlite: bool) -> None:  # noqa: ANN001
    """Backfill legacy columns on an already-initialized SQL database."""
    if sqlite:
        raise ValueError("SQLite migrations are handled in the SQLite transaction")
    if _target_is_hologres():
        for table, column, definition in _HOLO_ADDITIVE_COLUMNS:
            existing = {
                row[0]
                for row in conn.exec_driver_sql(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    """,
                    (table,),
                ).fetchall()
            }
            if existing and column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
    else:
        for table, columns in _LEGACY_MIGRATIONS.items():
            for name, definition in columns.items():
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}"
                )


def _reject_duplicates(conn, query: str, message: str) -> None:  # noqa: ANN001
    """Abort start-up when a uniqueness invariant cannot be enforced.

    Called before the matching index is created; the surrounding transaction
    rolls back so a refused database is left exactly as it was.
    """
    rows = conn.exec_driver_sql(query).fetchall()
    if not rows:
        return
    values = ", ".join(str(row[0]) for row in rows[:10])
    raise RuntimeError(message.format(values=values))


def init_schema(dsn: str = "") -> None:
    engine = _get_engine(dsn)
    if engine.dialect.name == "postgresql":
        is_hologres = "hologres.aliyuncs.com" in (engine.url.host or "").lower()
        token = _ddl_hologres.set(is_hologres)
        try:
            if is_hologres:
                metadata.create_all(engine)
                # Hologres rejects DML after DDL in the same transaction.
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    _migrate_columns(conn, False)
            else:
                with engine.begin() as conn:
                    conn.exec_driver_sql("SELECT pg_advisory_xact_lock(716234019)")
                    metadata.create_all(conn)
                    _migrate_columns(conn, False)
        finally:
            _ddl_hologres.reset(token)
        return

    metadata.create_all(engine)

    # SQLite-only one-time hygiene (column back-fills, de-dup, and the
    # uniqueness indexes that must be built after de-dup).
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        for table, columns in _LEGACY_MIGRATIONS.items():
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

        # Identity is unique per tenant. Drop idx_users_username_global if
        # present, then create the indexes below.
        conn.exec_driver_sql("DROP INDEX IF EXISTS idx_users_username_global")

        _reject_duplicates(
            conn,
            """
            SELECT tenant_id || '/' || username AS dup
            FROM users
            GROUP BY tenant_id, username
            HAVING COUNT(*) > 1
            ORDER BY dup
            """,
            "Cannot enforce per-tenant usernames because these tenant/username "
            "pairs are duplicated: {values}. Rename the duplicates before "
            "restarting Auth.",
        )
        # Local sign-in resolves an account by username alone, with no tenant to
        # narrow it down, so rows that carry a password stay globally unique.
        _reject_duplicates(
            conn,
            """
            SELECT username AS dup
            FROM users
            WHERE password_hash != ''
            GROUP BY username
            HAVING COUNT(*) > 1
            ORDER BY dup
            """,
            "Cannot keep password sign-in unambiguous because these usernames "
            "are shared by several password accounts: {values}. Rename the "
            "duplicates before restarting Auth.",
        )

        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tenant_username "
            "ON users(tenant_id, username)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_password_username "
            "ON users(username) WHERE password_hash != ''"
        )
        # A role has set semantics within one scope; collapse exact duplicates
        # before enforcing that invariant at the database boundary.
        conn.exec_driver_sql(
            """
            DELETE FROM role_bindings
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM role_bindings
                GROUP BY user_id, role, scope_type, scope_id
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_role_bindings_scope_unique
            ON role_bindings(user_id, role, scope_type, scope_id)
            """
        )
        conn.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_raw_cypher_grants_scope
            ON raw_cypher_grants(actor_user_id, tenant_id, workspace_id, expires_at)
            """
        )


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def query_one(conn: _Conn, statement, params: tuple = ()) -> dict[str, Any] | None:  # noqa: ANN001
    result = conn.execute(statement, params)
    row = result.mappings().first()
    return dict(row) if row is not None else None


def query_all(conn: _Conn, statement, params: tuple = ()) -> list[dict[str, Any]]:  # noqa: ANN001
    result = conn.execute(statement, params)
    return [dict(row) for row in result.mappings().all()]
