"""Verify catalog-backed datasource reading and pure RDB target conversion."""

from __future__ import annotations

import json

import pytest
from context_manager.auth import GraphScopeAuthority, ScopeBinding
from context_manager.sql import (
    DatasourceError,
    DatasourceErrorCode,
    RdbConnector,
    SqlQueryService,
)
from qwenpaw_data_auth.database import get_db

from cm.infrastructure.access.support import authorization
from cm.retrieval.sql.support import create_catalog

_ACCESS = GraphScopeAuthority()


@pytest.fixture
def catalog_dsn(tmp_path):
    return create_catalog(tmp_path)


def _config(**overrides: object) -> dict[str, object]:
    """Build one semantic_config-compatible PostgreSQL config."""
    return {
        "host": "postgres.internal",
        "port": 5432,
        "database": "analytics",
        "user": "reader",
        "password": "very-secret",
        **overrides,
    }


def _scope(
    datasource_id: str | None = "warehouse",
    *,
    zone: str = "metadata",
) -> ScopeBinding:
    """Issue one metadata read scope for the reader tests."""
    return _ACCESS.issue_read_scope(
        authorization(),
        datasource_id=datasource_id,
        zone=zone,
    )


@pytest.mark.parametrize(
    ("datasource_id", "zone"),
    [
        (None, "knowledge"),
        ("another-datasource", "metadata"),
    ],
)
def test_catalog_lookup_cannot_widen_the_issued_scope(datasource_id, zone):
    with pytest.raises(DatasourceError) as caught:
        SqlQueryService(_ACCESS, RdbConnector()).get_datasource(
            "warehouse", scope=_scope(datasource_id, zone=zone)
        )
    assert caught.value.code is DatasourceErrorCode.SCOPE_MISMATCH


def test_reader_observes_catalog_changes_without_graph_reimport(catalog_dsn):
    reader = SqlQueryService(_ACCESS, RdbConnector())
    assert (
        reader.get_datasource("warehouse", scope=_scope()).config["password"]
        == "very-secret"
    )
    with get_db(catalog_dsn) as connection:
        connection.execute(
            "UPDATE datasource SET config = ?, datasource_type = 'mysql' WHERE datasource_id = 'warehouse'",
            (json.dumps(_config(password="new-secret", port=3306)),),
        )
        connection.commit()
    current = reader.get_datasource("warehouse", scope=_scope())
    assert current.datasource_type == "mysql"
    assert current.to_rdb_target().config.password == "new-secret"
