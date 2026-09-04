"""新数据源类型：MySQL 兼容别名、单文件库（DuckDB/SQLite）、类型枚举端点。"""
from __future__ import annotations

import sqlite3

import duckdb
import pytest

from semantic_config.errors import BadRequestError
from semantic_config.models.datasource_config import (
    CONFIG_MODEL_BY_TYPE,
    DatasourceType,
    MYSQL_COMPATIBLE_TYPES,
    MysqlConfig,
    TYPE_LABELS,
    validate_config,
)
from semantic_config.services.connection_tester import (
    test_connection as run_connection_test,
)


def test_mysql_compatible_types_share_config_model() -> None:
    for ds_type in ("starrocks", "doris", "tidb"):
        member = DatasourceType(ds_type)
        assert member in MYSQL_COMPATIBLE_TYPES
        assert CONFIG_MODEL_BY_TYPE[member] is MysqlConfig
        normalized = validate_config(
            ds_type,
            {
                "host": "db.internal",
                "port": 9030,
                "database": "dw",
                "user": "reader",
                "password": "secret",
            },
        )
        assert normalized["port"] == 9030


def test_every_type_has_label_and_model() -> None:
    for member in DatasourceType:
        assert member in CONFIG_MODEL_BY_TYPE, member
        assert member in TYPE_LABELS, member


def test_file_types_validate_path_only() -> None:
    assert validate_config("duckdb", {"path": "/tmp/a.duckdb"}) == {
        "path": "/tmp/a.duckdb"
    }
    assert validate_config("sqlite", {"path": "/tmp/a.db"}) == {"path": "/tmp/a.db"}
    with pytest.raises(BadRequestError):
        validate_config("duckdb", {"path": "/tmp/a.duckdb", "host": "nope"})


async def test_duckdb_connection_test(tmp_path) -> None:
    db_path = tmp_path / "analytics.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE sales (id INTEGER, amount DOUBLE)")
    conn.close()

    result = await run_connection_test("duckdb", {"path": str(db_path)})
    assert result.success, result.message
    assert "analytics.duckdb" in result.message

    missing = await run_connection_test("duckdb", {"path": str(tmp_path / "nope.db")})
    assert not missing.success


async def test_sqlite_connection_test(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    result = await run_connection_test("sqlite", {"path": str(db_path)})
    assert result.success, result.message


async def test_types_endpoint() -> None:
    from semantic_config.routers.datasource_router import list_datasource_types

    body = await list_datasource_types()
    items = {item["type"]: item for item in body["items"]}
    assert set(items) == {t.value for t in DatasourceType}

    mysql = items["mysql"]
    assert mysql["label"] == "MySQL"
    field_by_name = {f["name"]: f for f in mysql["fields"]}
    assert field_by_name["port"]["default"] == 3306
    assert field_by_name["port"]["type"] == "integer"
    assert field_by_name["password"]["secret"] is True
    assert field_by_name["host"]["required"] is True

    duckdb_info = items["duckdb"]
    assert [f["name"] for f in duckdb_info["fields"]] == ["path"]

    starrocks = items["starrocks"]
    assert starrocks["label"] == "StarRocks"
    assert {f["name"] for f in starrocks["fields"]} == set(field_by_name)


# ---------------------------------------------------------------------------
# DuckDB adapter + registry aliases


@pytest.fixture
def duckdb_db(tmp_path):
    path = tmp_path / "shop.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, gmv DOUBLE)")
    conn.execute("CREATE TABLE customers (id INTEGER, name VARCHAR)")
    conn.execute("INSERT INTO orders VALUES (1, 1, 250.5), (2, 2, 80.0)")
    conn.close()
    return path


def test_duckdb_adapter_metadata_and_sql(duckdb_db) -> None:
    from context_manager.graph.adapters.duckdb_adapter import DuckDBConnector
    from context_manager.secrets.schemas import DuckdbConnection

    connector = DuckDBConnector(
        DuckdbConnection(type="duckdb", path=str(duckdb_db)), db_id="shop"
    )
    try:
        result = connector.test_connection()
        assert result.success
        assert result.tables_found == 2

        manifest = connector.extract_metadata([])
        assert manifest.schema == "main"
        assert {t.name for t in manifest.tables} == {"orders", "customers"}

        rows = connector.execute_sql("SELECT count(*) FROM orders")
        assert rows.rows[0][0] == 2
    finally:
        connector.close()


def test_duckdb_adapter_missing_file() -> None:
    from context_manager.graph.adapters.base import ConnectorError
    from context_manager.graph.adapters.duckdb_adapter import DuckDBConnector
    from context_manager.secrets.schemas import DuckdbConnection

    with pytest.raises(ConnectorError, match="不存在"):
        DuckDBConnector(
            DuckdbConnection(type="duckdb", path="/nonexistent/nope.duckdb"),
            db_id="shop",
        )


def test_registry_aliases_mysql_compatible_types() -> None:
    from context_manager.graph.adapters.registry import _FACTORIES, _register_builtins

    _register_builtins()
    for alias in ("starrocks", "doris", "tidb", "duckdb"):
        assert alias in _FACTORIES, alias


def test_executor_backend_resolution(monkeypatch, tmp_path) -> None:
    from context_manager.api import executor

    def fake_config(ds_type: str, path: str = ""):
        def loader(_ds_id: str):
            cfg = {"_datasource_type": ds_type}
            if path:
                cfg["path"] = path
            return cfg

        return loader

    from context_manager.api import datasource_active_api

    for ds_type, expected in (
        ("starrocks", "pymysql_direct"),
        ("doris", "pymysql_direct"),
        ("tidb", "pymysql_direct"),
        ("duckdb", "duckdb_direct"),
        ("sqlite", "sqlite_direct"),
        ("mysql", "pymysql_direct"),
        ("postgresql", "direct"),
    ):
        monkeypatch.setattr(
            datasource_active_api, "load_datasource_config", fake_config(ds_type)
        )
        assert executor._resolve_backend_for_datasource("ds1") == expected, ds_type


def test_executor_duckdb_reads_rows(monkeypatch, duckdb_db) -> None:
    from context_manager.api import datasource_active_api, executor

    monkeypatch.setattr(
        datasource_active_api,
        "load_datasource_config",
        lambda _ds: {"_datasource_type": "duckdb", "path": str(duckdb_db)},
    )
    result = executor._execute_sql_via_duckdb(
        "SELECT id FROM orders ORDER BY id", datasource_id="ds1"
    )
    assert result.error is None
    assert [r[0] for r in result.rows] == [1, 2]

    # 只读打开：写入必须失败。
    write = executor._execute_sql_via_duckdb(
        "INSERT INTO orders VALUES (3, 3, 1.0)", datasource_id="ds1"
    )
    assert write.error is not None
