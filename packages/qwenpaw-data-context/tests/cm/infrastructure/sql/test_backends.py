"""Verify SQLite reads and cloud SDK mapping without remote database access."""

import sqlite3

import pytest
from context_manager.sql import (
    RdbConnector,
    RdbTarget,
    SqliteConfig,
)


def target(config):
    return RdbTarget("tenant", "workspace", "source", config)


@pytest.fixture
def sqlite_file(tmp_path):
    path = tmp_path / "data #?.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE facts (id INTEGER, name TEXT)")
        db.executemany(
            "INSERT INTO facts VALUES (?, ?)", [(1, "一"), (2, "two"), (3, None)]
        )
    return path


@pytest.mark.parametrize("read_only", [True, False])
def test_sqlite_reads_existing_file_and_enforces_read_only(sqlite_file, read_only):
    source = target(SqliteConfig(str(sqlite_file), read_only=read_only))
    connector = RdbConnector()
    assert connector.probe(source).healthy
    with connector.connect(source) as connection:
        description, rows, count = connection.query(
            "SELECT * FROM facts ORDER BY id",
            max_rows=1,
            timeout_ms=1000,
        )
        assert [column[0] for column in description] == ["id", "name"]
        assert rows == ((1, "一"), (2, "two"))
        assert count == -1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.query("DELETE FROM facts", max_rows=1, timeout_ms=1000)
    with sqlite3.connect(sqlite_file) as db:
        assert db.execute("SELECT COUNT(*) FROM facts").fetchone() == (3,)


def test_sqlite_timeout_interrupts_and_clears_handler(sqlite_file, monkeypatch):
    ticks = iter([0, 1])
    monkeypatch.setattr(
        "context_manager.sql.connectors.sqlite.monotonic", lambda: next(ticks)
    )
    with RdbConnector().connect(target(SqliteConfig(str(sqlite_file)))) as connection:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            connection.query(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n",
                max_rows=1,
                timeout_ms=100,
            )
        cursor = connection.cursor()
        try:
            assert cursor.execute("SELECT 1").fetchone() == (1,)
        finally:
            cursor.close()
