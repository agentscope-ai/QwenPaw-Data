"""Verify admitted SQL execution through datasource and RDB boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Self
from unittest.mock import Mock

import pytest
from context_manager.auth import GraphScopeAuthority, ScopeBinding
from context_manager.sql import (
    RdbBackend,
    RdbConnector,
    SqlAdmissionError,
    SqlExecutionError,
    SqlQueryService,
)
from context_manager.sql.artifact import cleanup_sql_results, sql_result_path

from cm.infrastructure.access.support import authorization
from cm.retrieval.sql.support import create_catalog

_DATASOURCE_ID = "warehouse"
_ACCESS = GraphScopeAuthority()


class Column:
    """Model the psycopg named column-description shape."""

    def __init__(self, name: str) -> None:
        """Store one column name."""
        self.name = name


class FakeCursor:
    """Capture setup and query execution over deterministic rows."""

    def __init__(
        self,
        rows: Sequence[tuple[object, ...]],
        description: Sequence[object],
        *,
        fail_query: bool = False,
    ) -> None:
        """Initialize query results and lifecycle state."""
        self.rows = tuple(rows)
        self.rowcount = len(self.rows)
        self.description = tuple(description)
        self.fail_query = fail_query
        self.queries: list[str] = []
        self.fetch_sizes: list[int] = []
        self.close_count = 0

    def execute(self, query: str) -> None:
        """Record setup or user SQL and optionally simulate driver failure."""
        self.queries.append(query)
        if self.fail_query and query.startswith("SELECT"):
            raise RuntimeError(f"driver leaked query={query} password=very-secret")

    def fetchmany(self, size: int) -> Sequence[tuple[object, ...]]:
        """Return at most the requested number of configured rows."""
        self.fetch_sizes.append(size)
        return self.rows[:size]

    def close(self) -> None:
        """Count one cursor close."""
        self.close_count += 1

    def __enter__(self) -> Self:
        """Enter the cursor resource lifetime."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the cursor resource lifetime."""
        self.close()


class FakeDriverConnection:
    """Provide the DB-API transaction subset used by RdbConnection."""

    def __init__(self, cursor: FakeCursor) -> None:
        """Bind one fake cursor and empty lifecycle counters."""
        self.managed_cursor = cursor
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> FakeCursor:
        """Return the configured cursor."""
        return self.managed_cursor

    def commit(self) -> None:
        """Count a successful transaction."""
        self.commit_count += 1

    def rollback(self) -> None:
        """Count a failed transaction."""
        self.rollback_count += 1

    def close(self) -> None:
        """Count physical connection close."""
        self.close_count += 1


class CapturingFactory:
    """Return one driver connection and count physical opens."""

    def __init__(self, connection: FakeDriverConnection) -> None:
        """Bind the physical connection returned by every call."""
        self.connection = connection
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeDriverConnection:
        """Record driver options and return the configured connection."""
        self.calls.append(dict(kwargs))
        return self.connection


def _scope() -> ScopeBinding:
    """Issue one datasource-bound read scope."""
    return _ACCESS.issue_read_scope(
        authorization(),
        datasource_id="warehouse",
        zone="metadata",
    )


@pytest.fixture
def _executor(tmp_path_factory):
    """Compose a real catalog, mocking only the target driver."""
    catalog_path = tmp_path_factory.mktemp("sql-catalog")

    def build(cursor, *, backend=RdbBackend.POSTGRES):
        create_catalog(
            catalog_path,
            datasource_type=backend.value,
            config={
                "host": "database.internal",
                "port": 3306 if backend is RdbBackend.MYSQL else 5432,
                "database": "analytics",
                "user": "reader",
                "password": "very-secret",
            },
        )
        connection = FakeDriverConnection(cursor)
        factory = CapturingFactory(connection)
        connector = (
            RdbConnector(mysql_factory=factory)
            if backend is RdbBackend.MYSQL
            else RdbConnector(postgres_factory=factory)
        )
        return SqlQueryService(_ACCESS, connector), connection, factory

    return build


def test_postgres_query_is_read_only_bounded_and_json_safe(_executor) -> None:
    """The executor applies controls, fetches one extra row, and detaches values."""
    cursor = FakeCursor(
        (
            (Decimal("12.30"), date(2026, 8, 24), b"a"),
            (Decimal("45.60"), date(2026, 8, 25), b"b"),
        ),
        (Column("amount"), Column("day"), Column("payload")),
    )
    executor, connection, factory = _executor(cursor)
    scope = _scope()

    result = executor.execute(
        _DATASOURCE_ID,
        "SELECT amount, day, payload FROM facts",
        max_rows=1,
        timeout_ms=7500,
        scope=scope,
    )

    assert len(factory.calls) == 1
    assert cursor.queries == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 7500",
        "SELECT amount, day, payload FROM facts",
    ]
    assert cursor.fetch_sizes == [2]
    assert result.columns == ("amount", "day", "payload")
    assert result.rows == (("12.30", "2026-08-24", "YQ=="),)
    assert result.row_count == 1
    assert result.truncated is True
    assert result.elapsed_ms >= 0
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert cursor.close_count == 1
    assert connection.close_count == 1


def test_mysql_uses_backend_timeout_and_tuple_descriptions(_executor) -> None:
    """MySQL receives its own timeout command and standard tuple columns."""
    cursor = FakeCursor(((1,),), (("value", None, None),))
    executor, connection, _ = _executor(
        cursor,
        backend=RdbBackend.MYSQL,
    )

    result = executor.execute(
        _DATASOURCE_ID,
        "SELECT `value` FROM `facts`",
        max_rows=10,
        timeout_ms=1200,
        scope=_scope(),
    )

    assert cursor.queries == [
        "SET TRANSACTION READ ONLY",
        "SELECT /*+ MAX_EXECUTION_TIME(1200) */ `value` FROM `facts`",
    ]
    assert result.columns == ("value",)
    assert result.rows == ((1,),)
    assert result.truncated is False
    assert connection.commit_count == 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO facts VALUES (1)",
        "SELECT 1; DELETE FROM facts",
        "WITH removed AS (DELETE FROM facts RETURNING id) SELECT * FROM removed",
        "SELECT * INTO copied_facts FROM facts",
        "SELECT * FROM facts FOR UPDATE",
        "SHOW TABLES",
        "EXPLAIN SELECT * FROM facts",
        "SELECT '",
    ],
)
def test_forbidden_sql_is_rejected_before_connection(sql: str, _executor) -> None:
    """Non-query, multi-statement, write, and lock forms fail before execution."""
    cursor = FakeCursor((), ())
    executor, connection, factory = _executor(cursor)

    with pytest.raises(SqlAdmissionError):
        executor.execute(
            _DATASOURCE_ID,
            sql,
            max_rows=100,
            timeout_ms=1000,
            scope=_scope(),
        )

    assert factory.calls == []
    assert cursor.queries == []
    assert connection.close_count == 0


def test_driver_failure_is_sanitized_and_rolled_back(_executor) -> None:
    """Driver text, query text, and credentials never cross the service boundary."""
    sql = "SELECT private_value FROM secret_table"
    cursor = FakeCursor((), (Column("private_value"),), fail_query=True)
    executor, connection, _ = _executor(cursor)

    with pytest.raises(SqlExecutionError) as excinfo:
        executor.execute(
            _DATASOURCE_ID,
            sql,
            max_rows=10,
            timeout_ms=1000,
            scope=_scope(),
        )

    message = str(excinfo.value)
    assert sql not in message
    assert "very-secret" not in message
    assert message == "Relational SQL execution failed"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert cursor.close_count == 1
    assert connection.close_count == 1


@pytest.fixture
def artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "failure", ["fetch", "commit", "close", "cleanup", "write", "publish"]
)
def test_export_failures_leave_no_downloadable_file(
    artifact_root, monkeypatch, failure, _executor
):
    cursor = FakeCursor(((1,),), (("x",),))
    executor, connection, _ = _executor(cursor)

    def fail(*args, **kwargs):
        raise OSError("private path and password=secret")

    if failure == "fetch":
        monkeypatch.setattr(cursor, "fetchmany", fail)
    elif failure in {"commit", "close"}:
        monkeypatch.setattr(connection, failure, fail)
    elif failure == "cleanup":
        clock = Mock(return_value=100.0)
        monkeypatch.setattr("context_manager.sql.query.monotonic", clock)
        monkeypatch.setattr("context_manager.sql.query.cleanup_sql_results", fail)
    elif failure == "write":
        monkeypatch.setattr("context_manager.sql.artifact.csv.writer", fail)
    else:
        monkeypatch.setattr("pathlib.Path.replace", fail)
    with pytest.raises(SqlExecutionError, match="SQL CSV export failed") as error:
        executor.export_csv(
            _DATASOURCE_ID,
            "SELECT * FROM facts",
            max_rows=1,
            timeout_ms=1000,
            scope=_scope(),
        )
    assert "secret" not in str(error.value)
    assert not [path for path in artifact_root.rglob("*") if path.is_file()]
    if failure == "cleanup":
        assert connection.close_count == 1
        cleanup = Mock(wraps=cleanup_sql_results)
        monkeypatch.setattr("context_manager.sql.query.cleanup_sql_results", cleanup)
        clock.return_value = 160.0
        result = executor.export_csv(
            _DATASOURCE_ID,
            "SELECT * FROM facts",
            max_rows=1,
            timeout_ms=1000,
            scope=_scope(),
        )
        cleanup.assert_called_once()
        assert sql_result_path(result["result_id"], scope=_scope()).is_file()
