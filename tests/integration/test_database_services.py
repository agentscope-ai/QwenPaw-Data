"""Opt-in connectivity checks against real CI database services."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TypeVar

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("QWENPAW_DATA_INTEGRATION_DATABASES") != "1",
    reason="set QWENPAW_DATA_INTEGRATION_DATABASES=1 to run database integration tests",
)

T = TypeVar("T")


def _eventually(connect: Callable[[], T], *, timeout: float = 90.0) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return connect()
        except Exception as exc:  # Service containers may still be initializing.
            last_error = exc
            time.sleep(2)
    raise AssertionError(f"database did not become ready: {last_error}")


def test_postgres_service() -> None:
    import psycopg

    connection = _eventually(
        lambda: psycopg.connect(
            os.environ["QWENPAW_DATA_TEST_POSTGRES_DSN"],
            connect_timeout=5,
        ),
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


def test_mysql_service() -> None:
    import pymysql

    connection = _eventually(
        lambda: pymysql.connect(
            host=os.environ["QWENPAW_DATA_TEST_MYSQL_HOST"],
            user="root",
            password=os.environ["QWENPAW_DATA_TEST_MYSQL_PASSWORD"],
            database="qwenpaw_data",
            connect_timeout=5,
        ),
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


def test_postgres_executor_statement_timeout(monkeypatch) -> None:
    from psycopg.conninfo import conninfo_to_dict
    from context_manager.api import executor

    monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", 100)
    monkeypatch.setattr(
        executor,
        "_pg_connect_kwargs",
        lambda **kw: conninfo_to_dict(
            os.environ["QWENPAW_DATA_TEST_POSTGRES_DSN"],
        ),
    )
    result = executor._execute_sql_via_psycopg("SELECT pg_sleep(2)")
    assert result.error and result.error.startswith("QUERY_TIMEOUT:")
    assert executor._execute_sql_via_psycopg("SELECT 1").rows == [[1]]


@pytest.mark.parametrize("prefix", ["", "WITH probe AS (SELECT 1) "])
def test_mysql_server_honors_generated_timeout_hint(monkeypatch, prefix) -> None:
    import pymysql
    from context_manager.api import executor

    monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", 100)
    connection = pymysql.connect(
        host=os.environ["QWENPAW_DATA_TEST_MYSQL_HOST"],
        user="root",
        password=os.environ["QWENPAW_DATA_TEST_MYSQL_PASSWORD"],
        database="qwenpaw_data",
        connect_timeout=5,
        read_timeout=3,
    )
    with connection, connection.cursor() as cursor:
        # SLEEP alone returns 1 on interruption; WHERE makes it a statement error.
        sql = prefix + "SELECT value FROM (SELECT 1 AS value) AS t WHERE SLEEP(2) = 0"
        with pytest.raises(pymysql.err.OperationalError) as raised:
            cursor.execute(executor._mysql_with_timeout_hint(sql))
        assert raised.value.args[0] == 3024
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


def test_neo4j_service() -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["QWENPAW_DATA_TEST_NEO4J_URI"],
        auth=("neo4j", os.environ["QWENPAW_DATA_TEST_NEO4J_PASSWORD"]),
    )
    try:
        _eventually(driver.verify_connectivity)
        record = driver.execute_query("RETURN 1 AS value").records[0]
        assert record["value"] == 1
    finally:
        driver.close()
