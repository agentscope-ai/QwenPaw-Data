"""SQLite/BigQuery adapters and the UniversalConnector framework."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from context_manager.contracts.import_models import SourceConfig
from context_manager.graph.adapters.base import ConnectorError, ExecResult
from context_manager.graph.adapters.bigquery_adapter import (
    BigQueryConnector,
    _normalize_bq_type,
    _walk_fields,
)
from context_manager.graph.adapters.registry import get_adapter
from context_manager.graph.adapters.sqlite_adapter import SQLiteConnector
from context_manager.secrets.schemas import SqliteConnection


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            gmv REAL,
            ds TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE VIEW big_orders AS SELECT * FROM orders WHERE gmv > 100;
        INSERT INTO customers VALUES (1, '张三'), (2, '李四');
        INSERT INTO orders VALUES (1, 1, 250.5, '20260901'), (2, 2, 80.0, '20260901');
        """
    )
    conn.commit()
    conn.close()
    return path


def _connector(path: Path, **kwargs) -> SQLiteConnector:
    conn = SqliteConnection(type="sqlite", path=str(path), **kwargs)
    return SQLiteConnector(conn, db_id="shop")


def test_sqlite_test_connection(sqlite_db: Path) -> None:
    connector = _connector(sqlite_db)
    result = connector.test_connection()
    assert result.success
    assert result.tables_found == 2
    connector.close()


def test_sqlite_missing_file_raises() -> None:
    with pytest.raises(ConnectorError, match="不存在"):
        _connector(Path("/nonexistent/nope.sqlite"))


def test_sqlite_extract_metadata(sqlite_db: Path) -> None:
    connector = _connector(sqlite_db)
    manifest = connector.extract_metadata([])

    assert manifest.db_id == "shop"
    assert manifest.schema == "main"
    names = {t.name for t in manifest.tables}
    assert names == {"orders", "customers", "big_orders"}

    orders = next(t for t in manifest.tables if t.name == "orders")
    assert orders.key == "tbl:shop.main.orders"
    assert orders.partition_key == "ds"
    assert "CREATE TABLE" in orders.ddl

    view = next(t for t in manifest.tables if t.name == "big_orders")
    assert "SELECT" in view.ddl.upper()
    assert view.ddl.rstrip().endswith(";")

    id_col = next(
        c for c in manifest.columns if c.table == "orders" and c.name == "id"
    )
    assert id_col.pk is True
    assert id_col.key == "col:shop.main.orders.id"
    ds_col = next(
        c for c in manifest.columns if c.table == "orders" and c.name == "ds"
    )
    assert ds_col.is_partition is True

    fk = next(f for f in manifest.fks if f.src_table == "orders")
    assert (fk.src_col, fk.dst_table, fk.dst_col) == ("customer_id", "customers", "id")
    connector.close()


def test_sqlite_execute_sql_rows_and_truncation(sqlite_db: Path) -> None:
    connector = _connector(sqlite_db)
    result = connector.execute_sql("SELECT id, gmv FROM orders ORDER BY id")
    assert result.error is None
    assert result.columns == ["id", "gmv"]
    assert result.rows == [[1, 250.5], [2, 80.0]]

    truncated = connector.execute_sql("SELECT id FROM orders", max_rows=1)
    assert truncated.truncated is True
    assert len(truncated.rows) == 1

    failed = connector.execute_sql("SELECT nope FROM missing")
    assert failed.error is not None
    connector.close()


def test_sqlite_read_only_blocks_writes(sqlite_db: Path) -> None:
    connector = _connector(sqlite_db)  # read_only defaults True
    result = connector.execute_sql("DELETE FROM orders")
    assert result.error is not None
    # data intact
    check = connector.execute_sql("SELECT count(*) FROM orders")
    assert check.rows == [[2]]
    connector.close()


def test_registry_dispatches_sqlite(sqlite_db: Path) -> None:
    config = SourceConfig.model_validate(
        {"connection": {"type": "sqlite", "path": str(sqlite_db)}}
    )
    adapter = get_adapter(config, "shop")
    assert isinstance(adapter, SQLiteConnector)
    assert adapter.test_connection().success
    adapter.close()


def test_bigquery_from_config_rejects_wrong_connection(sqlite_db: Path) -> None:
    config = SourceConfig.model_validate(
        {"connection": {"type": "sqlite", "path": str(sqlite_db)}}
    )
    with pytest.raises(ConnectorError, match="BigQueryConnection"):
        BigQueryConnector.from_config(config, "bq")


def test_bigquery_missing_dependency_hint() -> None:
    pytest.importorskip("pydantic")
    try:
        import google.cloud.bigquery  # noqa: F401

        pytest.skip("google-cloud-bigquery installed; dependency hint not testable")
    except ImportError:
        pass
    config = SourceConfig.model_validate(
        {
            "connection": {
                "type": "bigquery",
                "project_id": "proj",
                "service_account_json": "{}",
            }
        }
    )
    with pytest.raises(ConnectorError, match="google-cloud-bigquery"):
        BigQueryConnector.from_config(config, "bq")


class _Field:
    def __init__(self, name, field_type="STRING", mode="NULLABLE", fields=()):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.fields = list(fields)
        self.description = ""


def test_bq_type_normalization_and_record_flattening() -> None:
    assert _normalize_bq_type(_Field("a", "INTEGER")) == "int64"
    assert _normalize_bq_type(_Field("a", "FLOAT")) == "float64"
    assert _normalize_bq_type(_Field("a", "STRING", mode="REPEATED")) == "array<string>"

    nested = _Field(
        "event_params",
        "RECORD",
        fields=[_Field("key"), _Field("value", "RECORD", fields=[_Field("int_value", "INTEGER")])],
    )
    names = [name for name, _ in _walk_fields([nested])]
    assert names == [
        "event_params",
        "event_params.key",
        "event_params.value",
        "event_params.value.int_value",
    ]


def test_exec_result_to_dict_jsonable() -> None:
    result = ExecResult(
        sql="SELECT 1",
        columns=["d", "n"],
        rows=[[datetime(2026, 9, 1, 8, 0), Decimal("1.5")]],
        row_count=1,
    )
    dumped = result.to_dict()
    assert dumped["rows"] == [["2026-09-01T08:00:00", 1.5]]
    assert "logview_url" not in dumped
