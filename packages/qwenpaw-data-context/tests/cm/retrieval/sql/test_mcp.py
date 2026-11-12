"""Verify SQL tool adaptation and authenticated datasource execution."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from context_manager.sql import (
    RdbConnector,
)
from qwenpaw_data_auth.database import get_db
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cm.retrieval.mcp_support import build_app, call_tool
from cm.retrieval.sql.support import create_catalog


@pytest.fixture(autouse=True)
def sql_result_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))


def sql_cursor(rows=((1,),), description=(("value",),)):
    cursor = MagicMock(
        spec=[
            "__enter__",
            "__exit__",
            "close",
            "description",
            "execute",
            "fetchmany",
            "rows",
            "rowcount",
        ]
    )
    cursor.__enter__.return_value = cursor
    cursor.description = description
    cursor.rows = rows
    cursor.rowcount = -1
    position = 0

    def fetchmany(size):
        nonlocal position
        batch = cursor.rows[position : position + size]
        position += len(batch)
        return batch

    cursor.fetchmany.side_effect = fetchmany
    return cursor


@pytest.fixture
def sql_app(tmp_path):
    # Only the physical DB-API boundary is mocked; the SQL service stays real.
    cursor = sql_cursor()
    connection = Mock(spec=["cursor", "commit", "rollback", "close"])
    connection.cursor.return_value = cursor
    factory = Mock(return_value=connection)
    dsn = create_catalog(
        tmp_path,
        config={
            "host": "database.invalid",
            "database": "analytics",
            "user": "reader",
            "password": "db-secret",
        },
    )
    app, store, runtime, _ = build_app(
        rdb_connector=RdbConnector(postgres_factory=factory, mysql_factory=factory),
        model_config_dsn=dsn,
    )
    app.state.test_catalog_dsn = dsn
    try:
        yield app, store, cursor, connection, factory
    finally:
        runtime.close()


def sql_result(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_http_sqlite_reads_unwoven_datasource_and_downloads_csv(sql_app, tmp_path):
    app, _, _, _, factory = sql_app
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE facts (id INTEGER)")
        db.executemany("INSERT INTO facts VALUES (?)", [(1,), (2,), (3,)])
    with get_db(app.state.test_catalog_dsn) as db:
        db.execute(
            "UPDATE datasource SET datasource_type = ?, config = ?",
            (
                "sqlite",
                json.dumps({"path": str(path)}),
            ),
        )
        db.commit()
    with TestClient(app) as client:
        result = sql_result(
            call_tool(
                client,
                "execute_read",
                {
                    "datasource_id": "warehouse",
                    "sql": "SELECT * FROM facts ORDER BY id",
                    "max_rows": 2,
                },
            )
        )
        assert result["rows"] == [[1], [2]]
        assert result["truncated"] is True
        download = client.get(
            result["download_url"], headers={"Authorization": "Bearer user-1"}
        )
        assert download.status_code == 200
        assert download.content == b"id\r\n1\r\n2\r\n"
        native = client.post(
            "/api/v1/rdb/execute_sql",
            json={
                "datasource_id": "warehouse",
                "sql": "SELECT count(*) AS total FROM facts",
            },
            headers={"Authorization": "Bearer user-1"},
        )
        assert native.status_code == 200, native.text
        assert native.json()["rows"] == [[3]]
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("token", "status"),
    [
        (None, 401),
        ("invalid", 401),
        ("other-workspace", 403),
        ("user-2", 200),
    ],
)
def test_sql_download_requires_authenticated_workspace_membership(
    sql_app, token, status
):
    app, _, _, _, factory = sql_app
    with TestClient(app) as client:
        result = sql_result(
            call_tool(
                client,
                "execute_read",
                {"datasource_id": "warehouse", "sql": "SELECT 1"},
            )
        )
        response = client.get(
            result["download_url"],
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        assert response.status_code == status
        assert (
            client.get(
                result["download_url"], headers={"Authorization": "Bearer user-1"}
            ).status_code
            == 200
        )
    factory.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 22, 30])
async def test_host_materializes_develop_bounded_csv_contract(
    sql_app, tmp_path, monkeypatch, count
):
    artifact = pytest.importorskip("qwenpaw_data.host.core.integrations.cm.sql_artifact")
    settings = pytest.importorskip("qwenpaw_data.host.core.settings")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://testserver")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "host"))
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "local")
    settings.clear_settings()
    app, _, cursor, _, factory = sql_app
    cursor.rows = [(index,) for index in range(count)]
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as client,
        ):
            response = await client.post(
                "/mcp/v1/cm",
                headers={
                    "Authorization": "Bearer user-1",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_read",
                        "arguments": {
                            "datasource_id": "warehouse",
                            "sql": "SELECT 1",
                            "max_rows": 25,
                        },
                    },
                },
            )
            result = sql_result(response)
            rewritten = json.loads(
                await artifact.materialize_execute_sql_result(
                    json.dumps(result),
                    artifact_dir=tmp_path / "host-artifacts",
                    access_token="user-1",
                    transport=httpx.ASGITransport(app),
                )
            )
        if count:
            (path,) = (tmp_path / "host-artifacts" / "data" / "raw").glob("*.csv")
            assert path.read_text().splitlines() == [
                "value",
                *(str(index) for index in range(min(count, 25))),
            ]
            assert rewritten == {
                name: value for name, value in result.items() if name != "download_url"
            } | {
                "file_path": str(path.resolve()),
            }
        else:
            assert rewritten == result
            assert rewritten["download_url"] is None
            assert not (tmp_path / "host-artifacts").exists()
        assert rewritten["rows"] == [[index] for index in range(min(count, 20))]
        assert rewritten["total_row_count"] == min(count, 26)
        assert rewritten["truncated"] is (count > 25)
        factory.assert_called_once()
    finally:
        settings.clear_settings()


@pytest.mark.asyncio
async def test_sdk_discovers_and_executes_sql(sql_app):
    app, _, _, connection, factory = sql_app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http,
        streamable_http_client(
            "http://testserver/mcp/v1/cm", http_client=http
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        assert "execute_read" in {
            tool.name for tool in (await session.list_tools()).tools
        }
        http.headers.update(
            {"Authorization": "Bearer user-1", "datasource_id": "warehouse"}
        )
        result = await session.call_tool(
            "execute_read", {"datasource_id": "warehouse", "sql": "SELECT 1"}
        )
        assert not result.isError
        assert json.loads(result.content[0].text)["rows"] == [[1]]
    factory.assert_called_once()
    connection.close.assert_called_once_with()


def test_http_sql_keeps_datasource_credentials_isolated_across_concurrent_requests(
    sql_app,
):
    app, _store, _, _, factory = sql_app
    for user in ("user-1", "user-2"):
        with get_db(app.state.test_catalog_dsn) as catalog_connection:
            catalog_connection.execute(
                "INSERT INTO datasource (tenant_id, workspace_id, datasource_id, datasource_name, datasource_type, config) "
                "VALUES ('tenant-1', 'workspace-1', ?, 'Warehouse', 'postgresql', ?)",
                (
                    user,
                    json.dumps(
                        {
                            "host": "database.invalid",
                            "database": user,
                            "user": user,
                            "password": f"{user}-secret",
                        }
                    ),
                ),
            )
            catalog_connection.commit()

    def connect(**config):
        cursor = sql_cursor(((config["user"],),), (("reader",),))
        connection = Mock()
        connection.cursor.return_value = cursor
        return connection

    factory.side_effect = connect
    with TestClient(app) as client:

        def query(user):
            own = sql_result(
                call_tool(
                    client,
                    "execute_read",
                    {
                        "datasource_id": user,
                        "sql": "SELECT current_user",
                    },
                    token=user,
                    datasource_id=user,
                )
            )
            other = "user-2" if user == "user-1" else "user-1"
            switched = call_tool(
                client,
                "execute_read",
                {
                    "datasource_id": other,
                    "sql": "SELECT current_user",
                },
                token=user,
                datasource_id=user,
            )
            assert sql_result(switched)["rows"] == [[other]]
            return user, own

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(query, ["user-1", "user-2"] * 2))
    for user, result in results:
        assert result["rows"] == [[user]]
    assert factory.call_count == 8
    for item in factory.call_args_list:
        assert item.kwargs["password"] == f"{item.kwargs['user']}-secret"
        assert item.kwargs["dbname"] == item.kwargs["user"]
