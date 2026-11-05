from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp
from unittest.mock import MagicMock

from context_manager.api import executor


class _FakeError(Exception):
    def __init__(self, msg: str, code=None) -> None:  # noqa: ANN001
        super().__init__(msg)
        self.args = (code, msg) if code is not None else (msg,)


class TestTimeoutMarker:
    def test_mysql_max_execution_time(self) -> None:
        marked = executor._timeout_marker(
            _FakeError(
                "Query execution was interrupted, max execution time exceeded", 3024
            ),
            "pymysql",
        )
        assert marked is not None and marked.startswith("QUERY_TIMEOUT:")

    def test_mysql_client_read_timeout(self) -> None:
        # pymysql raises (2013, 'Lost connection to MySQL server during query (timed out)')
        err = Exception()
        err.args = (2013, "Lost connection to MySQL server during query (timed out)")
        marked = executor._timeout_marker(err, "pymysql")
        assert marked is not None and marked.startswith("SQL_READ_TIMEOUT:")
        assert "cancellation is not confirmed" in marked

    def test_pg_statement_timeout(self) -> None:
        error = _FakeError("canceling statement due to statement timeout")
        error.sqlstate = "57014"
        marked = executor._timeout_marker(error, "psycopg")
        assert marked is not None and marked.startswith("QUERY_TIMEOUT:")

    def test_plain_sql_error_not_marked(self) -> None:
        assert (
            executor._timeout_marker(
                _FakeError('(1064, "You have an error in your SQL syntax")'), "pymysql"
            )
            is None
        )


class TestMysqlTimeoutHint:
    def test_hint_prefixes_select(self) -> None:
        out = executor._mysql_with_timeout_hint("SELECT a FROM t")
        assert out.startswith(
            f"SELECT /*+ MAX_EXECUTION_TIME({executor.SQL_STATEMENT_TIMEOUT_MS}) */"
        )

    def test_cte_outer_select_is_bounded(self) -> None:
        sql = "WITH x AS (SELECT 1) SELECT * FROM x"
        tree = sqlglot.parse_one(executor._mysql_with_timeout_hint(sql), read="mysql")
        assert "MAX_EXECUTION_TIME" in tree.args["hint"].sql()
        assert tree.args["with_"].expressions[0].this.args.get("hint") is None

    def test_existing_hint_replaced_once_without_changing_literals(self) -> None:
        sql = "SELECT /*+ MAX_EXECUTION_TIME(999999) NO_RANGE_OPTIMIZATION(t) */ 'SELECT * FROM t' FROM t"
        out = executor._mysql_with_timeout_hint(sql)
        assert out.count("MAX_EXECUTION_TIME") == 1
        assert "999999" not in out
        assert "NO_RANGE_OPTIMIZATION(t)" in out
        assert sqlglot.parse_one(out, read="mysql").expressions[
            0
        ] == exp.Literal.string("SELECT * FROM t")

    def test_union_first_select_is_bounded(self) -> None:
        tree = sqlglot.parse_one(
            executor._mysql_with_timeout_hint("SELECT 1 UNION SELECT 2"), read="mysql"
        )
        assert tree.this.args["hint"]
        assert not tree.expression.args.get("hint")

    def test_disabled_leaves_sql_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", 0)
        assert executor._mysql_with_timeout_hint("SELECT 1") == "SELECT 1"


@pytest.mark.parametrize(
    "error,backend",
    [
        (TimeoutError("connection timed out"), "psycopg"),
        (_FakeError("Can't connect (timed out)", 2003), "pymysql"),
        (_FakeError("canceling statement due to user request"), "psycopg"),
        (_FakeError("Lost connection during query", 2013), "pymysql"),
        (Exception(), "psycopg"),
        (_FakeError('column "statement timeout" does not exist'), "psycopg"),
    ],
)
def test_connection_and_user_cancellation_are_not_statement_timeouts(error, backend):
    assert executor._timeout_marker(error, backend) is None


@pytest.mark.parametrize("timeout", [0, 75])
def test_postgres_applies_budget_before_query_and_closes(monkeypatch, timeout):
    monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", timeout)
    monkeypatch.setattr(executor, "_pg_connect_kwargs", lambda **kw: {})
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.description = []
    cursor.rowcount = 0
    cursor.fetchmany.return_value = []
    monkeypatch.setattr(executor.psycopg, "connect", lambda **kw: conn)
    result = executor._execute_sql_via_psycopg("SELECT 1", datasource_id="a")
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements == ([f"SET statement_timeout = {timeout}"] if timeout else []) + [
        "SELECT 1"
    ]
    assert result.error is None
    conn.__exit__.assert_called_once()


@pytest.mark.parametrize("configured,expected", [(None, 0.1), (5, 0.1), (0.05, 0.05)])
def test_mysql_read_limit_and_cleanup_on_timeout(monkeypatch, configured, expected):
    import pymysql
    from context_manager.api import datasource_active_api

    monkeypatch.setattr(executor, "SQL_STATEMENT_TIMEOUT_MS", 100)
    monkeypatch.setattr(
        datasource_active_api,
        "resolve_mysql_connect_kwargs",
        lambda **kw: {"read_timeout": configured},
    )
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = pymysql.err.OperationalError(
        2013, "Lost connection during query (timed out)"
    )
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(pymysql, "connect", connect)
    result = executor._execute_sql_via_pymysql("SELECT 1", datasource_id="a")
    assert connect.call_args.kwargs["read_timeout"] == expected
    assert "MAX_EXECUTION_TIME(100)" in cursor.execute.call_args.args[0]
    assert result.error.startswith("SQL_READ_TIMEOUT:")
    conn.close.assert_called_once()
