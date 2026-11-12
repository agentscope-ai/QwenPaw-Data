"""Own CM's relational connection pools and startup schema initialization."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from threading import RLock

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.selectable import TableClause

from .schema.relational import external_tables, metadata
from .settings import get_settings
from .settings.db import DbSettings
from .utils.validation import require_trimmed_text


class CMDatabase:
    """Own the shared SQL engine for one service lifetime."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._lock = RLock()

    @classmethod
    def from_settings(cls, settings: DbSettings) -> CMDatabase:
        """Create the service pool, adapting Host's async driver once."""
        parsed = cls._sync_url(settings.url)
        if parsed.get_backend_name() == "sqlite":
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            options = (
                {}
                if parsed.database not in (None, "", ":memory:")
                else {"poolclass": StaticPool}
            )
            engine = create_engine(
                parsed, connect_args={"check_same_thread": False}, **options
            )
            event.listen(
                engine,
                "connect",
                partial(
                    cls._configure_sqlite,
                    busy_timeout_ms=settings.sqlite_busy_timeout_ms,
                ),
            )
        else:
            engine = create_engine(
                parsed,
                pool_pre_ping=True,
                pool_recycle=settings.pool_recycle_seconds,
            )
        return cls(engine)

    @staticmethod
    def _configure_sqlite(connection, _record, *, busy_timeout_ms: int) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @contextmanager
    def read_connection(self) -> Iterator[Connection]:
        """Borrow a read-only transaction and restore the pool's write access."""
        sqlite = self.engine.dialect.name == "sqlite"
        with self.engine.connect() as connection:
            sqlite_query_only = False
            try:
                if sqlite:
                    sqlite_query_only = bool(
                        connection.exec_driver_sql("PRAGMA query_only").scalar_one()
                    )
                    connection.exec_driver_sql("PRAGMA query_only = ON")
                    connection.exec_driver_sql("BEGIN")
                else:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                yield connection
            finally:
                connection.rollback()
                if sqlite:
                    connection.exec_driver_sql(
                        "PRAGMA query_only = " + ("ON" if sqlite_query_only else "OFF")
                    )

    @staticmethod
    def _sync_url(dsn: str) -> URL:
        """Adapt Host's shared URL to synchronous CM reads and writes."""
        value = require_trimmed_text(dsn, "QWENPAW_DATA_DB_URL")
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("QWENPAW_DATA_DB_URL must be a database URL") from None
        backend = url.get_backend_name()
        if backend not in {"sqlite", "postgresql"}:
            raise ValueError("QWENPAW_DATA_DB_URL must use SQLite or PostgreSQL/Hologres")
        query = dict(url.query)
        if backend == "postgresql" and "ssl" in query:
            mode = query.pop("ssl")
            if mode not in (
                "disable",
                "allow",
                "prefer",
                "require",
                "verify-ca",
                "verify-full",
            ):
                raise ValueError("QWENPAW_DATA_DB_URL ssl must be a PostgreSQL SSL mode")
            if "sslmode" in query and query["sslmode"] != mode:
                raise ValueError("QWENPAW_DATA_DB_URL ssl and sslmode must agree")
            query["sslmode"] = mode
        return url.set(
            drivername="sqlite+pysqlite"
            if backend == "sqlite"
            else "postgresql+psycopg",
            query=query,
        )

    def initialize(self) -> None:
        """Create absent CM tables and report missing columns before serving."""
        hologres = "hologres.aliyuncs.com" in (self.engine.url.host or "")
        with self._lock:
            if self.engine.dialect.name == "postgresql" and not hologres:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": 0x434D4350},
                    )
                    self._initialize_tables(connection, hologres=False)
            else:
                with self.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as connection:
                    self._initialize_tables(connection, hologres=hologres)

    @staticmethod
    def _initialize_tables(connection, *, hologres: bool) -> None:
        from context_manager.migration.stateless import (
            copy_inflight_cm_jobs,
            ensure_recovery_run_columns,
        )

        ensure_recovery_run_columns(connection)
        for table in metadata.sorted_tables:
            if not inspect(connection).has_table(table.name):
                ddl = CreateTable(table, if_not_exists=True)
                # Compile locally so another package's global DDL hooks cannot select our backend.
                compiler = connection.dialect.ddl_compiler(connection.dialect, ddl)
                sql = compiler.visit_create_table(ddl)
                orientation = table.info.get("holo_orientation")
                if hologres and orientation:
                    sql = sql.rstrip() + f" WITH (orientation='{orientation}')"
                connection.exec_driver_sql(sql)
                if hologres:
                    connection.rollback()
                    connection.invalidate()  # Hologres refreshes table metadata on a new session.
            columns = {
                column["name"] for column in inspect(connection).get_columns(table.name)
            }
            missing = set(table.columns.keys()) - columns
            if missing:
                raise RuntimeError(
                    f"CM table {table.name} is missing columns: {', '.join(sorted(missing))}"
                )
        copy_inflight_cm_jobs(connection)

    def validate_dependencies(self, *, history_source: str) -> None:
        """Check external tables required by the selected history integration."""
        if history_source not in {"sql", "remote"}:
            raise ValueError("history_source must be sql or remote")
        from .schema.relational import host_history_tables

        history_names = {table.name for table in host_history_tables}
        required = (
            external_tables
            if history_source == "sql"
            else tuple(table for table in external_tables if table.name not in history_names)
        )
        try:
            with self.engine.connect() as connection:
                self.validate_connection(connection, required)
        except SQLAlchemyError:
            raise RuntimeError("external dependency schema inspection failed") from None

    @staticmethod
    def validate_connection(
        connection: Connection,
        tables: Iterable[TableClause],
    ) -> None:
        """Validate declared external columns through SQLAlchemy inspection."""
        inspector = inspect(connection)
        errors = []
        for table in tables:
            if inspector.has_table(table.name):
                columns = {
                    column["name"] for column in inspector.get_columns(table.name)
                }
            else:
                columns = set()
            if not columns:
                errors.append(f"Required external table {table.name} is missing")
                continue
            missing = set(table.columns.keys()) - columns
            if missing:
                names = ", ".join(f"{table.name}.{name}" for name in sorted(missing))
                errors.append(
                    f"External table {table.name} is missing columns: {names}"
                )
        if errors:
            raise RuntimeError("; ".join(errors))

    def close(self) -> None:
        """Release the pool after its consumers have stopped."""
        self.engine.dispose()


_database: CMDatabase | None = None


def initialize_database(settings: DbSettings | None = None) -> CMDatabase:
    """Open the service database and initialize CM-owned tables at startup."""
    global _database
    if _database is not None:
        raise RuntimeError("CMDatabase is already initialized")
    database = CMDatabase.from_settings(settings or get_settings().db)
    try:
        database.initialize()
    except Exception:
        database.close()
        raise
    _database = database
    return database


def get_database() -> CMDatabase:
    """Return the database opened by service startup."""
    if _database is None:
        raise RuntimeError("CMDatabase has not been initialized")
    return _database


def close_database() -> None:
    """Release the service database at shutdown."""
    global _database
    if _database is not None:
        _database.close()
        _database = None
