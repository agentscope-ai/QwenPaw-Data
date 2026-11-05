import pytest
import sqlglot
from sqlglot import exp

from context_manager.view_fallthrough import (
    FallthroughGiveUp,
    extract_cm_view_refs,
    extract_missing_table_name,
    rewrite_cm_view_to_physical,
    rewrite_dataset_to_physical,
)


def test_same_leaf_target_is_not_rewritten_twice_or_in_literals():
    sql = "SELECT orders, 'db.orders' AS label FROM db.orders /* orders */"
    out = rewrite_dataset_to_physical(sql, "orders", ("analytics", "orders"))
    tree = sqlglot.parse_one(out)
    assert [part.name for part in tree.find(exp.Table).parts] == ["analytics", "orders"]
    assert tree.expressions[0].name == "orders"
    assert tree.find(exp.Literal).this == "db.orders"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH orders AS (SELECT 1 AS value) SELECT * FROM orders",
        "SELECT 'cm_view.orders' AS orders FROM other_table",
    ],
)
def test_cte_and_non_table_occurrences_are_untouched(sql):
    assert rewrite_dataset_to_physical(sql, "orders", ("analytics", "sales")) == sql


@pytest.mark.parametrize(
    "query,expected",
    [
        ("SELECT orders.value, 'db.orders' FROM db.orders", [(7, "db.orders")]),
        ("SELECT db.orders.value FROM db.orders", [(7,)]),
        ("SELECT x.value FROM db.orders AS x", [(7,)]),
        ("SELECT orders.* FROM db.orders", [(7,)]),
        ("SELECT db.orders.* FROM db.orders", [(7,)]),
        (
            "WITH orders AS (SELECT 99 AS value) SELECT orders.value, x.value FROM orders CROSS JOIN db.orders AS x",
            [(99, 7)],
        ),
        (
            "SELECT x.value FROM db.orders x WHERE EXISTS (SELECT 1 WHERE x.value = 7)",
            [(7,)],
        ),
    ],
)
def test_rewritten_qualifiers_execute_without_changing_results(query, expected):
    import duckdb

    with duckdb.connect() as conn:
        conn.execute("CREATE SCHEMA analytics")
        conn.execute("CREATE TABLE analytics.sales AS SELECT 7 AS value")
        rewritten = rewrite_dataset_to_physical(
            query, "orders", ("analytics", "sales"), dialect="duckdb"
        )
        assert conn.execute(rewritten).fetchall() == expected


def test_cm_view_references_are_ast_scoped_and_replacements_atomic():
    sql = "SELECT 'cm_view.fake' FROM cm_view.a JOIN cm_view.b ON true"
    assert extract_cm_view_refs(sql) == {"a", "b"}
    out = rewrite_cm_view_to_physical(
        sql, {"a": [("public", "b")], "b": [("public", "c")]}
    )
    tree = sqlglot.parse_one(out)
    assert [t.name for t in tree.find_all(exp.Table)] == ["b", "c"]
    assert tree.find(exp.Literal).this == "cm_view.fake"


@pytest.mark.parametrize(
    "sql", ["SELECT * FROM", "SELECT 1; SELECT 2", "DELETE FROM orders"]
)
def test_unsafe_or_unparsed_rewrite_fails_closed(sql):
    with pytest.raises(FallthroughGiveUp):
        rewrite_dataset_to_physical(sql, "orders", ("public", "sales"))


@pytest.mark.parametrize(
    "message",
    [
        'relation "orders" does not exist',
        'relation "db.orders" does not exist',
        "Table 'db.orders' doesn't exist",
        "Catalog Error: Table with name orders does not exist!",
    ],
)
def test_missing_table_message_variants(message):
    assert extract_missing_table_name(message) == "orders"


def test_fallthrough_resolves_on_active_datasource_and_can_be_disabled(monkeypatch):
    from context_manager.api import executor

    calls, scopes = [], []
    monkeypatch.setattr(
        executor, "_resolve_backend_for_datasource", lambda _: "duckdb_direct"
    )

    def execute(sql, **kw):
        calls.append(sql)
        if len(calls) == 1:
            return executor.ExecResult(
                sql=sql, error='relation "orders" does not exist'
            )
        return executor.ExecResult(sql=sql, rows=[[7]], row_count=1)

    def parents(names, *, datasource_id, dialect):
        scopes.append(datasource_id)
        return {"orders": [("analytics", "sales")]}

    monkeypatch.setattr(executor, "_execute_sql_via_duckdb", execute)
    monkeypatch.setattr(executor, "_resolve_dataset_parents", parents)
    assert executor.execute_sql("SELECT * FROM orders", datasource_id="a").rows == [[7]]
    assert scopes == ["a"]
    calls.clear()
    scopes.clear()
    result = executor.execute_sql(
        "SELECT * FROM orders", datasource_id="a", allow_fallthrough=False
    )
    assert result.error
    assert len(calls) == 1 and scopes == []


def test_parent_lookup_scopes_datasource_and_rejects_filtered_views(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from neo4j import GraphDatabase
    from context_manager.api import executor

    calls, closed = [], []

    def run(query, **params):
        calls.append((query, params))
        return [
            {
                "name": "orders",
                "schema": "analytics",
                "table": "sales",
                "sql": "SELECT * FROM analytics.sales",
                "filter_summary": "",
            },
            {
                "name": "filtered",
                "schema": "analytics",
                "table": "sales",
                "sql": "SELECT * FROM analytics.sales WHERE value > 0",
                "filter_summary": "",
            },
        ]

    driver = SimpleNamespace(
        session=lambda **kw: nullcontext(SimpleNamespace(run=run)),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(GraphDatabase, "driver", lambda *args, **kw: driver)
    result = executor._resolve_dataset_parents(
        {"orders", "filtered"}, datasource_id="a", dialect="duckdb"
    )
    assert result == {"orders": [("analytics", "sales")], "filtered": []}
    assert "datasource_id: $datasource_id" in calls[0][0]
    assert calls[0][1]["datasource_id"] == "a"
    assert closed == [True]
