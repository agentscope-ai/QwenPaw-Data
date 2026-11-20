"""Verify typed writes against a real SQLite source, including commits and rollback."""

import sqlite3

import pytest
from context_manager.auth import GraphScopeAuthority, WriteOwner
from context_manager.sql import (
    RdbConnector,
    SqlExecutionError,
    SqlQueryService,
)

from cm.infrastructure.access.support import authorization
from cm.retrieval.sql.support import create_catalog


@pytest.fixture
def writer(tmp_path):
    source = tmp_path / "business.sqlite"
    sqlite3.connect(source).close()
    create_catalog(
        tmp_path,
        datasource_type="sqlite",
        config={"path": str(source), "read_only": False},
    )
    access = GraphScopeAuthority()
    scope = access.issue_write_scope(
        authorization(role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )
    service = SqlQueryService(access, RdbConnector())

    def execute(sql, kind="modify", max_rows=20, capability=scope):
        return service.execute_write(
            "warehouse",
            sql,
            statement_type=kind,
            max_rows=max_rows,
            timeout_ms=1000,
            scope=capability,
        )

    return service, access, scope, execute


def test_create_insert_update_and_read_share_one_catalog_and_commit(writer):
    service, _, scope, execute = writer
    assert (
        execute(
            "CREATE TABLE sales(id INTEGER PRIMARY KEY, amount INTEGER)", "create"
        ).columns
        == ()
    )
    result = execute(
        "INSERT INTO sales VALUES (1,100),(2,200),(3,300) RETURNING id", max_rows=1
    )
    assert result.rows == ((1,),) and result.truncated
    assert result.row_count == 3  # Full mutation commits despite truncated RETURNING.
    assert execute("UPDATE sales SET amount=amount+10 WHERE id=1").row_count == 1
    result = execute(
        "UPDATE sales SET amount=amount WHERE id>1 RETURNING id", max_rows=1
    )
    assert result.row_count == 2 and result.truncated
    assert len(result.rows) == 1
    result = execute("UPDATE sales SET amount=amount WHERE id=99 RETURNING id")
    assert result.row_count == 0 and result.rows == () and not result.truncated
    result = service.execute(
        "warehouse",
        "SELECT SUM(amount) AS gmv FROM sales",
        max_rows=20,
        timeout_ms=1000,
        scope=scope,
    )
    assert result.rows == ((610,),)
    execute("CREATE VIEW totals AS SELECT SUM(amount) AS gmv FROM sales", "create")
    execute("CREATE TABLE copied AS SELECT * FROM sales", "create")
    assert service.execute(
        "warehouse",
        "SELECT count(*) FROM copied",
        max_rows=1,
        timeout_ms=1000,
        scope=scope,
    ).rows == ((3,),)


def test_failed_mutation_rolls_back_and_does_not_report_success(writer):
    service, _, scope, execute = writer
    execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)", "create")
    execute("INSERT INTO facts VALUES (1)")
    with pytest.raises(SqlExecutionError):
        execute("INSERT INTO facts VALUES (2),(1)")
    assert service.execute(
        "warehouse",
        "SELECT id FROM facts ORDER BY id",
        max_rows=10,
        timeout_ms=1000,
        scope=scope,
    ).rows == ((1,),)


def test_personal_graph_write_capability_does_not_authorize_database_writes(writer):
    _, access, _, execute = writer
    for scope in (
        access.issue_read_scope(authorization()),
        access.issue_write_scope(authorization(), owner=WriteOwner.USER),
        GraphScopeAuthority().issue_write_scope(
            authorization(role="ws_admin"), owner=WriteOwner.WORKSPACE
        ),
    ):
        with pytest.raises(PermissionError):
            execute("CREATE TABLE forbidden(id INT)", "create", capability=scope)
