from __future__ import annotations

import pytest

from context_manager.api import executor


class _FakeError(Exception):
    def __init__(self, msg: str, code=None) -> None:  # noqa: ANN001
        super().__init__(msg)
        self.args = (code, msg) if code is not None else (msg,)


class TestTimeoutMarker:
    def test_mysql_max_execution_time(self) -> None:
        marked = executor._timeout_marker(
            _FakeError("Query execution was interrupted, max execution time exceeded", 3024),
            "pymysql",
        )
        assert marked is not None and marked.startswith("QUERY_TIMEOUT:")

    def test_mysql_client_read_timeout(self) -> None:
        # pymysql raises (2013, 'Lost connection to MySQL server during query (timed out)')
        err = Exception()
        err.args = (2013, "Lost connection to MySQL server during query (timed out)")
        marked = executor._timeout_marker(err, "pymysql")
        assert marked is not None and marked.startswith("QUERY_TIMEOUT:")

    def test_pg_statement_timeout(self) -> None:
        marked = executor._timeout_marker(
            _FakeError("canceling statement due to statement timeout"), "psycopg"
        )
        assert marked is not None and marked.startswith("QUERY_TIMEOUT:")

    def test_plain_sql_error_not_marked(self) -> None:
        assert executor._timeout_marker(
            _FakeError("(1064, \"You have an error in your SQL syntax\")"), "pymysql"
        ) is None


class TestMysqlTimeoutHint:
    def test_hint_prefixes_select(self) -> None:
        out = executor._mysql_with_timeout_hint("SELECT a FROM t")
        assert out.startswith(f"SELECT /*+ MAX_EXECUTION_TIME({executor.SQL_STATEMENT_TIMEOUT_MS}) */")

    def test_cte_not_rewritten(self) -> None:
        sql = "WITH x AS (SELECT 1) SELECT * FROM x"
        assert executor._mysql_with_timeout_hint(sql) == sql

    def test_disabled_leaves_sql_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", 0)
        assert executor._mysql_with_timeout_hint("SELECT 1") == "SELECT 1"
