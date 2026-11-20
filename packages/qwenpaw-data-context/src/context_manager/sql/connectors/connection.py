"""Own relational driver creation, transaction, probe, and close lifecycles."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from math import ceil
from threading import Lock
from typing import Any, Self

from ..config import (
    BigQueryConfig,
    DuckDbConfig,
    MySqlConfig,
    MySqlSslMode,
    OdpsConfig,
    PostgresConfig,
    RdbBackend,
    RdbTarget,
    SqliteConfig,
)

ConnectionFactory = Callable[..., Any]
_ODPS_BACKENDS = (RdbBackend.ODPS,)
# StarRocks and Doris speak the MySQL protocol but have no read-only
# transactions; their connections autocommit and use session query_timeout.
_ANALYTIC_MYSQL_BACKENDS = (RdbBackend.STARROCKS, RdbBackend.DORIS)
# These connections are autocommit or read-only, so transaction() sends no
# COMMIT/ROLLBACK and only bounds the cursor lifecycle.
_NO_TRANSACTION_BACKENDS = (*_ANALYTIC_MYSQL_BACKENDS, RdbBackend.DUCKDB)


class RdbError(RuntimeError):
    """Hide driver failures behind a credential-free relational error."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        target: RdbTarget,
    ) -> None:
        """Create one sanitized error with stable routing metadata."""
        super().__init__(message)
        self.operation = operation
        self.backend = target.backend
        self.datasource_id = target.datasource_id


@dataclass(frozen=True, slots=True)
class RdbProbe:
    """Report backend-neutral connection liveness without driver details."""

    healthy: bool
    backend: RdbBackend
    datasource_id: str


class RdbConnection:
    """Own one native connection or SDK client and its query lifecycle."""

    def __init__(self, raw_connection: Any, target: RdbTarget) -> None:
        """Bind an opened driver connection to its immutable target."""
        if raw_connection is None:
            raise TypeError("raw_connection is required")
        if type(target) is not RdbTarget:
            raise TypeError("target must be an RdbTarget")
        self._raw_connection = raw_connection
        self._target = target
        self._state_lock = Lock()
        self._closed = False
        self._transaction_active = False

    @property
    def target(self) -> RdbTarget:
        """Return the exact datasource route fixed at connection creation."""
        return self._target

    @property
    def native_client(self) -> Any:
        """Borrow the driver/SDK client for operations not expressible as SQL.

        The caller must stay within this connection's lifetime and must not close
        the borrowed client. Backend-specific metadata interpretation stays outside RDB.
        """
        self._ensure_open("native_client")
        return self._raw_connection

    @property
    def closed(self) -> bool:
        """Return whether this wrapper has reached terminal close."""
        with self._state_lock:
            return self._closed

    def cursor(self) -> Any:
        """Open a driver cursor for an internal repository or use case."""
        self._ensure_open("cursor")
        if self._target.backend in (*_ODPS_BACKENDS, RdbBackend.BIGQUERY):
            raise self._error(
                "This backend exposes query jobs instead of cursors", "cursor"
            )
        try:
            return self._raw_connection.cursor()
        except Exception as exc:
            raise self._error("Could not open relational cursor", "cursor") from exc

    @contextmanager
    def transaction(self) -> Iterator[RdbConnection]:
        """Commit one callback block or roll it back on failure."""
        self._begin_transaction()
        try:
            yield self
        except BaseException:
            self._finish_transaction(commit=False)
            raise
        else:
            self._finish_transaction(commit=True)

    def query(self, sql: str, *, max_rows: int, timeout_ms: int):
        """Return (description, rows, total) with one extra row for truncation."""
        self._ensure_open("query")
        backend = self._target.backend
        if backend in _ODPS_BACKENDS:
            from .odps import query

            return query(self._raw_connection, sql, max_rows=max_rows)
        if backend is RdbBackend.BIGQUERY:
            from .bigquery import query

            return query(
                self._raw_connection,
                self._target.config,
                sql,
                max_rows=max_rows,
            )
        if backend is RdbBackend.DUCKDB:
            from .duckdb import query

            return query(
                self._raw_connection, sql, max_rows=max_rows, timeout_ms=timeout_ms
            )
        with self.transaction(), closing(self.cursor()) as cursor:
            try:
                if backend is RdbBackend.SQLITE:
                    from .sqlite import configure_read

                    configure_read(self._raw_connection, cursor, timeout_ms)
                elif backend in _ANALYTIC_MYSQL_BACKENDS:
                    cursor.execute(f"SET query_timeout = {ceil(timeout_ms / 1000)}")
                else:
                    # TiDB rejects READ ONLY as a disabled no-op; SQL admission
                    # and the MAX_EXECUTION_TIME hint still apply.
                    if backend is not RdbBackend.TIDB:
                        cursor.execute("SET TRANSACTION READ ONLY")
                    if backend in (RdbBackend.POSTGRES, RdbBackend.HOLOGRES):
                        cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                cursor.execute(sql)
                return (
                    cursor.description,
                    tuple(cursor.fetchmany(max_rows + 1)),
                    cursor.rowcount,
                )
            finally:
                if backend is RdbBackend.SQLITE:
                    self._raw_connection.set_progress_handler(None, 0)

    def ping(self) -> None:
        """Run the protocol-neutral liveness query on the active connection."""
        try:
            self._ensure_open("ping")
            if self._target.backend in _ODPS_BACKENDS:
                from .odps import probe

                probe(self._raw_connection, self._target.config)
                return
            if self._target.backend is RdbBackend.BIGQUERY:
                from .bigquery import probe

                probe(self._raw_connection)
                return
            with closing(self.cursor()) as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
                if row is None or not row or row[0] != 1:
                    raise RdbError(
                        "Relational health check returned an unexpected result",
                        operation="ping",
                        target=self._target,
                    )
        except RdbError:
            raise
        except Exception as exc:
            raise self._error("Relational health check failed", "ping") from exc

    def execute(self, sql: str, *, max_rows: int, timeout_ms: int):
        """Commit one admitted write and return bounded RETURNING rows, if any."""
        self._ensure_open("execute")
        backend = self._target.backend
        if backend in _ODPS_BACKENDS:
            from .odps import execute

            return execute(self._raw_connection, sql)
        if backend is RdbBackend.BIGQUERY:
            from .bigquery import execute

            return execute(
                self._raw_connection, self._target.config, sql, max_rows=max_rows
            )
        if backend is RdbBackend.DUCKDB:
            raise self._error("DuckDB datasources are read-only", "execute")
        with self.transaction(), closing(self.cursor()) as cursor:
            try:
                if backend is RdbBackend.SQLITE:
                    from .sqlite import configure_timeout

                    configure_timeout(self._raw_connection, cursor, timeout_ms)
                elif backend in (RdbBackend.POSTGRES, RdbBackend.HOLOGRES):
                    cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                cursor.execute(sql)
                description = cursor.description
                # Drain RETURNING before committing (required by SQLite). Retain
                # only max_rows+1 rows while preserving the driver's affected count.
                rows = []
                if description:
                    while batch := cursor.fetchmany(1000):
                        rows.extend(batch[: max(0, max_rows + 1 - len(rows))])
                affected = cursor.rowcount
                if backend is RdbBackend.SQLITE and description:
                    # Some SQLite drivers leave rowcount stale after RETURNING.
                    # Read the statement's count after draining its result set.
                    cursor.execute("SELECT changes()")
                    affected = cursor.fetchone()[0]
                return description or (), tuple(rows), affected
            finally:
                if backend is RdbBackend.SQLITE:
                    self._raw_connection.set_progress_handler(None, 0)

    def close(self) -> None:
        """Close the physical connection exactly once."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            # PyODPS owns its HTTP session pool and exposes no client close API.
            if self._target.backend not in _ODPS_BACKENDS:
                self._raw_connection.close()
        except Exception as exc:
            raise self._error("Could not close relational connection", "close") from exc

    def __enter__(self) -> Self:
        """Enter this connection's resource lifetime."""
        self._ensure_open("enter")
        return self

    def __exit__(self, *args: object) -> None:
        """Close this connection when its resource lifetime ends."""
        self.close()

    def _begin_transaction(self) -> None:
        """Reject closed or nested transaction blocks."""
        if self._target.backend in (*_ODPS_BACKENDS, RdbBackend.BIGQUERY):
            raise self._error(
                "Query jobs do not support this transaction API", "transaction"
            )
        with self._state_lock:
            if self._closed:
                raise self._error("Relational connection is closed", "transaction")
            if self._transaction_active:
                raise self._error(
                    "Nested relational transactions are not supported",
                    "transaction",
                )
            self._transaction_active = True

    def _finish_transaction(self, *, commit: bool) -> None:
        """Finish the active driver transaction and restore wrapper state."""
        operation = "commit" if commit else "rollback"
        try:
            if self._target.backend not in _NO_TRANSACTION_BACKENDS:
                action = (
                    self._raw_connection.commit
                    if commit
                    else self._raw_connection.rollback
                )
                action()
        except Exception as exc:
            raise self._error(f"Relational {operation} failed", operation) from exc
        finally:
            with self._state_lock:
                self._transaction_active = False

    def _ensure_open(self, operation: str) -> None:
        """Reject work after terminal close."""
        with self._state_lock:
            if self._closed:
                raise self._error("Relational connection is closed", operation)

    def _error(self, message: str, operation: str) -> RdbError:
        """Create a sanitized error for this connection target."""
        return RdbError(message, operation=operation, target=self._target)


class RdbConnector:
    """Create scoped relational connections through finite driver adapters."""

    def __init__(
        self,
        *,
        postgres_factory: ConnectionFactory | None = None,
        mysql_factory: ConnectionFactory | None = None,
    ) -> None:
        """Accept optional factories for composition and deterministic tests."""
        self._postgres_factory = postgres_factory
        self._mysql_factory = mysql_factory

    def connect(self, target: RdbTarget) -> RdbConnection:
        """Open exactly one physical connection for a trusted target."""
        if type(target) is not RdbTarget:
            raise TypeError("target must be an RdbTarget")
        try:
            if type(target.config) is PostgresConfig:
                raw_connection = self._connect_postgres(target.config)
            elif type(target.config) is MySqlConfig:
                raw_connection = self._connect_mysql(target.config)
            elif isinstance(target.config, OdpsConfig):
                from .odps import connect

                raw_connection = connect(target.config)
            elif isinstance(target.config, BigQueryConfig):
                from .bigquery import connect

                raw_connection = connect(target.config)
            elif isinstance(target.config, SqliteConfig):
                from .sqlite import connect

                raw_connection = connect(target.config)
            elif isinstance(target.config, DuckDbConfig):
                from .duckdb import connect

                raw_connection = connect(target.config)
            else:
                raise TypeError("unsupported relational connection config")
        except RdbError:
            raise
        except Exception as exc:
            raise RdbError(
                "Could not connect to relational datasource",
                operation="connect",
                target=target,
            ) from exc
        return RdbConnection(raw_connection, target)

    def probe(self, target: RdbTarget) -> RdbProbe:
        """Open, ping, and close one connection without leaking driver errors."""
        try:
            with self.connect(target) as connection:
                connection.ping()
        except RdbError:
            return RdbProbe(False, target.backend, target.datasource_id)
        return RdbProbe(True, target.backend, target.datasource_id)

    def _connect_postgres(self, config: PostgresConfig) -> Any:
        """Map validated PostgreSQL configuration to psycopg."""
        factory = self._postgres_factory
        if factory is None:
            import psycopg

            factory = psycopg.connect
        options: dict[str, object] = {}
        if config.ssl_root_cert_file is not None:
            options["sslrootcert"] = config.ssl_root_cert_file
        return factory(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            dbname=config.database,
            connect_timeout=config.connect_timeout,
            sslmode=config.ssl_mode.value,
            autocommit=False,
            **options,
        )

    def _connect_mysql(self, config: MySqlConfig) -> Any:
        """Map validated MySQL-protocol configuration to PyMySQL."""
        factory = self._mysql_factory
        if factory is None:
            import pymysql

            factory = pymysql.connect
        ssl_options: dict[str, object] = {}
        if config.ssl_mode is MySqlSslMode.DISABLED:
            ssl_options["ssl_disabled"] = True
        elif config.ssl_mode is MySqlSslMode.PREFERRED:
            ssl_options["ssl_disabled"] = False
        elif config.ssl_mode is MySqlSslMode.REQUIRED:
            ssl_options["ssl"] = {"check_hostname": False}
        else:
            ssl_options.update(
                ssl_ca=config.ssl_ca_file,
                ssl_verify_cert=True,
                ssl_verify_identity=(config.ssl_mode is MySqlSslMode.VERIFY_IDENTITY),
            )
        timeouts: dict[str, object] = {}
        if config.read_timeout is not None:
            # Client waiting bound only; it does not confirm server cancellation.
            timeouts["read_timeout"] = config.read_timeout
        return factory(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset=config.charset,
            connect_timeout=config.connect_timeout,
            autocommit=config.backend in _ANALYTIC_MYSQL_BACKENDS,
            **ssl_options,
            **timeouts,
        )


__all__ = ["RdbConnection", "RdbConnector", "RdbError", "RdbProbe"]
