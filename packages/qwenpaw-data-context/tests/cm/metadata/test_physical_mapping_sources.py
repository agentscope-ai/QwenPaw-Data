"""Backend catalog reflection uses the current RDB lifecycle and source identity."""

from unittest.mock import Mock

import pytest
from context_manager.metadata.physical.source import PhysicalMappingSource
from context_manager.sql import (
    MySqlConfig,
    PostgresConfig,
    RdbBackend,
    RdbConnector,
    RdbTarget,
)


def target(config):
    return RdbTarget("tenant", "workspace", "source", config)


@pytest.mark.parametrize(
    "backend", [RdbBackend.POSTGRES, RdbBackend.HOLOGRES, RdbBackend.MYSQL]
)
def test_relational_catalog_maps_primary_nullability_comment_and_types(backend):
    raw, cursor = Mock(), Mock()
    raw.cursor.return_value = cursor
    mysql = backend is RdbBackend.MYSQL
    cursor.fetchall.side_effect = [
        [("orders",)],
        [("region", "VARCHAR(40)", "PRI", "NO", " 地区 ")]
        if mysql
        else [("region", "character varying(40)", True, False, " 地区 ")],
    ]
    config = (
        MySqlConfig("host", "db", "user", "secret")
        if mysql
        else PostgresConfig("host", "db", "user", "secret", backend=backend)
    )
    factory = Mock(return_value=raw)
    connector = RdbConnector(
        **{("mysql_factory" if mysql else "postgres_factory"): factory}
    )
    source = PhysicalMappingSource(connector, target(config), {}, ("source",))
    manifest = source.extract_metadata(["sales"])
    assert [(table.schema, table.name) for table in manifest.tables] == [
        ("sales", "orders")
    ]
    column = manifest.columns[0]
    assert (column.type, column.pk, column.nullable, column.comment) == (
        "varchar(40)",
        True,
        False,
        "地区",
    )
    assert cursor.execute.call_args_list[0].args[1] == ("sales",)
    assert cursor.execute.call_args_list[1].args[1] == ("sales", "orders")
    raw.close.assert_called_once()
