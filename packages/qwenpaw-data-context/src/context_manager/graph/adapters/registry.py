"""Adapter 注册表:按 ``config.connection.type``(string) 查找 SourceAdapter 工厂。

Breaking change(2026-06-04):discriminator 从 ``SourceConfig.type`` 迁到
``SourceConfig.connection.type``,registry key 从 ``SourceType`` enum 改为 plain str
(``TypedConnection`` 子类的 ``Literal['postgres']`` 等 discriminator 值)。
"""
from __future__ import annotations

from typing import Callable

from ...contracts.import_models import SourceConfig
from .base import SourceAdapter

_FACTORIES: dict[str, Callable[[SourceConfig, str], SourceAdapter]] = {}


def register_adapter(
    source_type: str,
    factory: Callable[[SourceConfig, str], SourceAdapter],
) -> None:
    _FACTORIES[source_type] = factory


def get_adapter(config: SourceConfig, db_id: str) -> SourceAdapter:
    conn_type = config.connection.type
    factory = _FACTORIES.get(conn_type)
    if factory is None:
        available = ", ".join(sorted(_FACTORIES))
        raise ValueError(
            f"no adapter registered for connection type {conn_type!r}; "
            f"available: {available}"
        )
    return factory(config, db_id)


def _register_builtins() -> None:
    from .postgres_adapter import PostgresAdapter
    from .mysql_adapter import MySQLAdapter
    from .ddl_adapter import DDLAdapter
    from .csv_adapter import CSVAdapter
    from .odps_adapter import OdpsAdapter
    from .sqlite_adapter import SQLiteConnector
    from .duckdb_adapter import DuckDBConnector
    from .bigquery_adapter import BigQueryConnector

    register_adapter("postgres", PostgresAdapter.from_config)
    register_adapter("hologres", PostgresAdapter.from_config)
    register_adapter("mysql", MySQLAdapter.from_config)
    # MySQL 线协议兼容引擎：information_schema 反射与 MySQL 相同。
    register_adapter("starrocks", MySQLAdapter.from_config)
    register_adapter("doris", MySQLAdapter.from_config)
    register_adapter("tidb", MySQLAdapter.from_config)
    register_adapter("odps", OdpsAdapter.from_config)
    register_adapter("ddl", DDLAdapter.from_config)
    register_adapter("csv", CSVAdapter.from_config)
    register_adapter("sqlite", SQLiteConnector.from_config)
    register_adapter("duckdb", DuckDBConnector.from_config)
    register_adapter("bigquery", BigQueryConnector.from_config)


_register_builtins()
