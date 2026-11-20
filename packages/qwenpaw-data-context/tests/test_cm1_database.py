"""Exercise CM table ownership, migrations and real SQLite read transactions."""

import json
import time
import traceback

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

import context_manager.db as db_module
from context_manager.auth.repository import OwnerRepository
from context_manager.db import CMDatabase, close_database, get_database, initialize_database
from context_manager.migration.stateless import ensure_deployed_stateless_columns
from context_manager.schema.relational import external_tables, host_history_tables, metadata
from context_manager.settings.db import DbSettings


@pytest.fixture
def database():
    result = CMDatabase.from_settings(DbSettings(url="sqlite+aiosqlite:///:memory:"))
    yield result
    result.close()


def create_dependency_fixtures(database, *, history=False):
    """Supply test-only SQL fixtures; production CM never creates these tables."""
    history_names = {table.name for table in host_history_tables}
    with database.engine.begin() as connection:
        for table in external_tables:
            if not history and table.name in history_names:
                continue
            columns = ", ".join(f'"{column.name}" TEXT' for column in table.columns)
            connection.exec_driver_sql(f'CREATE TABLE "{table.name}" ({columns})')


@pytest.mark.parametrize("dsn,driver", [
    ("sqlite:///:memory:", "sqlite+pysqlite"),
    ("sqlite+aiosqlite:///:memory:", "sqlite+pysqlite"),
    ("postgresql://user:test-password@localhost/db", "postgresql+psycopg"),
    ("postgresql+asyncpg://user:test-password@localhost/db", "postgresql+psycopg"),
    ("postgresql+psycopg2://user:test-password@localhost/db", "postgresql+psycopg"),
])
def test_sync_driver_translation_preserves_database_and_credentials(dsn, driver):
    url = CMDatabase._sync_url(dsn)
    assert url.drivername == driver
    assert url.database in {":memory:", "db"}
    if url.get_backend_name() == "postgresql":
        assert url.username == "user" and url.password == "test-password"


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_postgres_ssl_alias_is_translated(mode):
    url = CMDatabase._sync_url(f"postgresql+asyncpg://localhost/db?ssl={mode}&application_name=context")
    assert url.query == {"sslmode": mode, "application_name": "context"}
    assert CMDatabase._sync_url(f"postgresql://localhost/db?ssl={mode}&sslmode={mode}").query == {"sslmode": mode}


@pytest.mark.parametrize("dsn", [
    "not-a-url-test-password", "mysql://user:test-password@localhost/db",
    "postgresql://user:test-password@localhost/db?ssl=test-password",
    "postgresql://user:test-password@localhost/db?ssl=require&sslmode=disable",
    " sqlite:///:memory:",
])
def test_invalid_urls_are_sanitized(dsn):
    with pytest.raises(ValueError) as error:
        CMDatabase._sync_url(dsn)
    assert "test-password" not in str(error.value)
    assert "QWENPAW_DATA_DB_URL" in str(error.value)


@pytest.mark.parametrize("failure", [False, True])
def test_read_transaction_blocks_writes_and_restores_pool(database, failure):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sample (value TEXT)")
        connection.exec_driver_sql("INSERT INTO sample VALUES ('keep')")
    with pytest.raises((OperationalError, LookupError)):
        with database.read_connection() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT value FROM sample").scalar_one() == "keep"
            if failure:
                raise LookupError("caller interrupted")
            connection.exec_driver_sql("INSERT INTO sample VALUES ('denied')")
    with database.engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
        connection.exec_driver_sql("INSERT INTO sample VALUES ('allowed')")
    with database.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM sample").scalar_one() == 2


def test_read_transaction_restores_preexisting_query_only(database):
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
    try:
        with database.read_connection() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    finally:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only = OFF")


def test_sqlite_connection_pragmas(database):
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_initialize_is_idempotent_and_preserves_old_history_and_graph_tables(database):
    with database.engine.begin() as connection:
        for name in ("sessions", "legacy_graph"):
            connection.exec_driver_sql(f"CREATE TABLE {name} (value TEXT)")
            connection.exec_driver_sql(f"INSERT INTO {name} VALUES ('keep')")
    database.initialize()
    database.initialize()
    assert set(inspect(database.engine).get_table_names()) == set(metadata.tables) | {
        "sessions", "legacy_graph"
    }
    with database.read_connection() as connection:
        for name in ("sessions", "legacy_graph"):
            assert connection.exec_driver_sql(f"SELECT value FROM {name}").scalar_one() == "keep"
    assert not inspect(database.engine).has_table("chats")


def test_history_source_is_explicit_and_remote_only_excludes_host_history(database):
    create_dependency_fixtures(database)
    database.validate_dependencies(history_source="remote")
    with pytest.raises(RuntimeError) as error:
        database.validate_dependencies(history_source="sql")
    for table in host_history_tables:
        assert f"Required external table {table.name} is missing" in str(error.value)
    with pytest.raises(TypeError):
        database.validate_dependencies()
    with pytest.raises(ValueError):
        database.validate_dependencies(history_source="disabled")
    database.initialize()
    assert not inspect(database.engine).has_table("sessions")


@pytest.mark.parametrize("table_name", [
    table.name for table in external_tables if table not in host_history_tables
])
def test_remote_history_still_validates_every_other_external_table(database, table_name):
    create_dependency_fixtures(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'DROP TABLE "{table_name}"')
    with pytest.raises(RuntimeError, match=f"Required external table {table_name} is missing"):
        database.validate_dependencies(history_source="remote")
    assert not inspect(database.engine).has_table(table_name)


def test_sql_history_validates_actual_external_columns_without_repair(database):
    create_dependency_fixtures(database, history=True)
    database.validate_dependencies(history_source="sql")
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE chat_events")
        connection.exec_driver_sql("CREATE TABLE chat_events (chat_id TEXT)")
    database.initialize()
    with pytest.raises(RuntimeError, match="chat_events.*missing columns.*chat_events.payload"):
        database.validate_dependencies(history_source="sql")
    assert [column["name"] for column in inspect(database.engine).get_columns("chat_events")] == ["chat_id"]


def test_dependency_driver_errors_hide_credentials(database, monkeypatch):
    def broken_inspection(connection):
        raise OperationalError("password=test-password", {"secret": "test-password"}, Exception("test-password"))

    monkeypatch.setattr(db_module, "inspect", broken_inspection)
    with pytest.raises(RuntimeError, match="schema inspection failed") as error:
        database.validate_dependencies(history_source="remote")
    rendered = "".join(traceback.format_exception(error.value))
    assert "test-password" not in rendered
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_existing_recovery_table_gets_only_missing_ownership_columns(database):
    recovery = metadata.tables["cm_recovery_runs"]
    columns = ", ".join(
        f'"{column.name}" TEXT' for column in recovery.columns
        if column.name not in {"heartbeat_at", "executor_pod"}
    )
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE TABLE cm_recovery_runs ({columns})")
        connection.exec_driver_sql(
            "INSERT INTO cm_recovery_runs (tenant_id, workspace_id, task_type, task_id, rid, recovery_state) "
            "VALUES ('tenant', 'workspace', 'test', '1', 'rid', 'REGISTERED')"
        )
    database.initialize()
    database.initialize()
    assert {column["name"] for column in inspect(database.engine).get_columns("cm_recovery_runs")} == set(recovery.columns.keys())
    with database.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT rid FROM cm_recovery_runs").scalar_one() == "rid"


def test_stateless_repairs_are_idempotent_and_soft_delete_duplicate_live_rows(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE weave_task (id INTEGER)")
        connection.exec_driver_sql("CREATE TABLE probe_task (id INTEGER)")
        connection.exec_driver_sql(
            "CREATE TABLE explore_scope (id INTEGER, tenant_id TEXT, workspace_id TEXT, datasource_id TEXT, is_deleted INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE explore_state (id INTEGER, tenant_id TEXT, workspace_id TEXT, datasource_id TEXT, table_name TEXT, is_deleted INTEGER)"
        )
        for number in (1, 2):
            connection.exec_driver_sql(f"INSERT INTO explore_scope VALUES ({number}, 't', 'w', 'd', 0)")
            connection.exec_driver_sql(f"INSERT INTO explore_state VALUES ({number}, 't', 'w', 'd', 'table', 0)")
        ensure_deployed_stateless_columns(connection)
        ensure_deployed_stateless_columns(connection)
    with database.read_connection() as connection:
        for table in ("explore_scope", "explore_state"):
            assert connection.exec_driver_sql(f"SELECT id, is_deleted FROM {table} ORDER BY id").all() == [(1, 0), (2, 1)]
    columns = {column["name"] for column in inspect(database.engine).get_columns("weave_task")}
    assert {"heartbeat_at", "executor_pod", "cancel_requested"} <= columns
    assert "run_epoch" in {column["name"] for column in inspect(database.engine).get_columns("explore_scope")}


def test_inflight_job_copy_preserves_legacy_rows_and_is_idempotent(database):
    now = time.time()
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE cm_job (job_kind TEXT, job_key TEXT, tenant_id TEXT, workspace_id TEXT, "
            "user_id TEXT, status TEXT, progress TEXT, heartbeat_at DOUBLE, executor_pod TEXT)"
        )
        for key, heartbeat in (("fresh", now), ("stale", now - 600)):
            connection.execute(text(
                "INSERT INTO cm_job VALUES ('doc_ingest', :key, 't', 'w', :key, 'building', :progress, :heartbeat, 'worker')"
            ), {"key": key, "heartbeat": heartbeat, "progress": json.dumps({"filename": key + ".txt"})})
        connection.execute(text(
            "INSERT INTO cm_job VALUES ('vector_rebuild', 'rebuild', 't', 'w', 'alice', 'running', :progress, :heartbeat, 'worker')"
        ), {"heartbeat": now, "progress": json.dumps({"job_id": "job-1", "config_snapshot": {"embed_dim": 8}})})
    database.initialize()
    database.initialize()
    with database.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM cm_job").scalar_one() == 3
        assert connection.exec_driver_sql("SELECT user_id, status FROM cm_kg_documents ORDER BY user_id").all() == [
            ("fresh", "processing"), ("stale", "failed")
        ]
        assert connection.exec_driver_sql("SELECT user_id, run_epoch FROM cm_kg_user_claim").all() == [("fresh", 1)]
        assert connection.exec_driver_sql("SELECT job_id, status FROM cm_embedding_jobs").all() == [("job-1", "running")]


def test_owner_repository_uses_real_db_rbac_and_excludes_inactive_targets(database, monkeypatch):
    from qwenpaw_data_auth.database import metadata as auth_metadata, role_bindings, tenants, users, workspaces

    auth_metadata.create_all(database.engine, tables=[tenants, workspaces, users, role_bindings])
    with database.engine.begin() as connection:
        connection.execute(tenants.insert(), {"id": "t", "name": "Tenant", "neo4j_database": "graph"})
        connection.execute(workspaces.insert(), {"id": "w", "tenant_id": "t", "name": "Workspace", "instance_status": "ready"})
        connection.execute(users.insert(), {"id": "alice", "tenant_id": "t", "username": "Alice"})
        connection.execute(role_bindings.insert(), [
            {"id": "r1", "user_id": "alice", "role": "member", "scope_type": "workspace", "scope_id": "w"},
            {"id": "r2", "user_id": "alice", "role": "tenant_admin", "scope_type": "tenant", "scope_id": "t"},
            {"id": "r3", "user_id": "alice", "role": "ws_admin", "scope_type": "workspace", "scope_id": "other"},
        ])
    monkeypatch.setattr(db_module, "_database", database)
    repository = OwnerRepository()
    assert repository.get_username("t", "alice") == "Alice"
    assert repository.get_username("other", "alice") is None
    assert {row["role"] for row in repository.list_workspace_roles("t", "w", "alice")} == {"member", "tenant_admin"}
    assert repository.list_workspace_roles("other", "w", "alice") == []
    with database.engine.begin() as connection:
        connection.execute(users.update().values(status="disabled"))
    assert repository.list_workspace_roles("t", "w", "alice") == []


def test_database_singleton_startup_and_close_are_explicit(monkeypatch):
    monkeypatch.setattr(db_module, "_database", None)
    with pytest.raises(RuntimeError, match="not been initialized"):
        get_database()
    try:
        database = initialize_database(DbSettings(url="sqlite:///:memory:"))
        assert get_database() is database
        with pytest.raises(RuntimeError, match="already initialized"):
            initialize_database(DbSettings(url="sqlite:///:memory:"))
    finally:
        close_database()
        close_database()
    with pytest.raises(RuntimeError, match="not been initialized"):
        get_database()
