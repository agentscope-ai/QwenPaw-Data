"""Opt-in connectivity checks against real CI database services."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TypeVar

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("DATAPAW_INTEGRATION_DATABASES") != "1",
    reason="set DATAPAW_INTEGRATION_DATABASES=1 to run database integration tests",
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
            os.environ["DATAPAW_TEST_POSTGRES_DSN"],
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
            host=os.environ["DATAPAW_TEST_MYSQL_HOST"],
            user="root",
            password=os.environ["DATAPAW_TEST_MYSQL_PASSWORD"],
            database="datapaw",
            connect_timeout=5,
        ),
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


def test_neo4j_service() -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["DATAPAW_TEST_NEO4J_URI"],
        auth=("neo4j", os.environ["DATAPAW_TEST_NEO4J_PASSWORD"]),
    )
    try:
        _eventually(driver.verify_connectivity)
        record = driver.execute_query("RETURN 1 AS value").records[0]
        assert record["value"] == 1
    finally:
        driver.close()
