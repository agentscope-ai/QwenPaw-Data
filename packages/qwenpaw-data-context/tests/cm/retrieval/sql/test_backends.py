"""Exercise new datasource types through catalog resolution and SQL services."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest
import sqlglot
from context_manager.auth import GraphScopeAuthority
from context_manager.sql import (
    RdbConnector,
    SqlQueryService,
)

from cm.infrastructure.access.support import authorization
from cm.retrieval.sql.support import create_catalog

_ACCESS = GraphScopeAuthority()


def service(tmp_path, backend, config):
    _dsn = create_catalog(tmp_path, datasource_type=backend, config=config)
    return SqlQueryService(_ACCESS, RdbConnector())


def scope(user_id="user-1"):
    return _ACCESS.issue_read_scope(
        authorization(user_id),
        datasource_id="warehouse",
        zone="metadata",
    )


@pytest.mark.parametrize("backend", ["odps", "bigquery"])
def test_cloud_query_and_csv_through_catalog(tmp_path, monkeypatch, backend):
    if backend == "bigquery":
        pytest.importorskip(
            "google.cloud.bigquery",
            reason="install qwenpaw-data-context[bigquery] for BigQuery coverage",
        )
    config = {
        "project": "p",
        "endpoint": "https://endpoint",
        "access_key_id": "ak",
        "access_key_secret": "sk",
        "credentials_json": "{}",
    }
    executor = service(tmp_path, backend, config)
    parse = Mock(wraps=sqlglot.parse)
    monkeypatch.setattr(sqlglot, "parse", parse)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    client = Mock()
    if backend == "bigquery":
        result = MagicMock(schema=[SimpleNamespace(name="value")], total_rows=3)
        result.__iter__.side_effect = lambda: iter([(1,), (2,), (3,)])
        client.query.return_value.result.return_value = result
        monkeypatch.setattr(
            "context_manager.sql.connectors.bigquery.connect", lambda config: client
        )
        sql = "SELECT * EXCEPT(secret) FROM `p.dataset.facts`"
    else:
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.schema.columns = [SimpleNamespace(name="value")]
        reader.count = 3
        reader.__iter__.side_effect = lambda: iter(
            [SimpleNamespace(values=[n]) for n in (1, 2, 3)]
        )
        client.run_sql.return_value.open_reader.return_value = reader
        monkeypatch.setattr(
            "context_manager.sql.connectors.odps.connect", lambda config: client
        )
        sql = "SELECT value FROM source_project.facts"
    result = executor.execute(
        "warehouse", sql, max_rows=1, timeout_ms=1000, scope=scope()
    )
    exported = executor.export_csv(
        "warehouse", sql, max_rows=1, timeout_ms=1000, scope=scope()
    )
    dialect = "bigquery" if backend == "bigquery" else "hive"
    assert parse.call_args_list == [call(sql, read=dialect), call(sql, read=dialect)]
    assert result.columns == ("value",)
    assert result.rows == ((1,),)
    assert result.truncated
    assert exported["rows"] == [(1,)]
    assert exported["truncated"]
    assert exported["total_row_count"] == 3
    assert exported["result_id"]
