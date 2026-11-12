"""Opt-in real Neo4j/Holo AGE, authenticated MCP and SQL write/read journey.

CM_MCP_NEO4J_URI / CM_MCP_NEO4J_PASSWORD identify a disposable local Community
instance. CM_MCP_HOLOAGE_* selects Holo. Each run owns one UUID Workspace and
cleans only its records and AGE graphs; no temporary query accounts are used.
"""

import asyncio
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.db import close_database, initialize_database
from context_manager.runtime import (
    ContextRuntime,
    HoloConnectionConfig,
    Neo4jConnectionConfig,
    create_context_runtime,
)
from context_manager.schema import DATA_SOURCE, HAS_CALIBER, METRIC, METRIC_CALIBER
from context_manager.settings import get_settings
from context_manager.settings.cron import CronSettings
from context_manager.settings.db import DbSettings
from qwenpaw_data.host.core.db.tables import Base as HostBase
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db
from qwenpaw_data_auth.token import TokenIssuer
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.test_startup import _seed_member


@dataclass
class LiveCM:
    client: httpx.Client
    url: str
    token: str = field(repr=False)
    dsn: str
    backend: str
    secret: str = field(repr=False)
    runtime: ContextRuntime = field(repr=False)
    graph_name: str


@pytest.fixture(params=["neo4j", "holoage"])
def live_cm(tmp_path, request):
    backend = request.param
    workspace = "cmtools" + uuid4().hex[:12]
    driver = holo_cleanup = runtime = server = thread = None
    holo_parameters = None
    graphs = [workspace, f"tenant-1_{workspace}_user-1", f"tenant-1_{workspace}_user-2"]
    database = os.environ.get("CM_MCP_NEO4J_DATABASE", "neo4j")
    if backend == "neo4j":
        uri = os.environ.get("CM_MCP_NEO4J_URI")
        if not uri:
            pytest.skip("set CM_MCP_NEO4J_URI/PASSWORD for real Neo4j validation")
        assert urlparse(uri).hostname in {"localhost", "127.0.0.1", "::1"}
        password = os.environ["CM_MCP_NEO4J_PASSWORD"]
        driver = GraphDatabase.driver(uri, auth=("neo4j", password))
        graph_connection = Neo4jConnectionConfig(
            tenant_id="tenant-1",
            workspace_id=workspace,
            neo4j_uri=uri,
            neo4j_user="neo4j",
            neo4j_password=password,
            neo4j_database=database,
        )
    else:
        if not os.environ.get("CM_MCP_HOLOAGE_HOST"):
            pytest.skip("set CM_MCP_HOLOAGE_HOST/PORT/USER/PASSWORD/DATABASE")
        graph = get_settings().graph
        for name, suffix in (
            ("host", "HOST"),
            ("port", "PORT"),
            ("user", "USER"),
            ("password", "PASSWORD"),
            ("database", "DATABASE"),
        ):
            value = os.environ["CM_MCP_HOLOAGE_" + suffix]
            setattr(graph, name, int(value) if name == "port" else value)
        holo_parameters = graph.connection_params()
        graph_connection = HoloConnectionConfig(
            tenant_id="tenant-1", workspace_id=workspace, graph_name=workspace
        )
    try:
        if holo_parameters is not None:
            holo_cleanup = psycopg.connect(**holo_parameters, autocommit=True)
        dsn = f"sqlite:///{tmp_path / 'auth.sqlite'}"
        host_engine = create_engine(dsn)
        try:
            HostBase.metadata.create_all(host_engine)
        finally:
            host_engine.dispose()
        _seed_member(dsn)
        with get_db(dsn) as sql:
            sql.execute(
                "UPDATE workspaces SET id=? WHERE id='workspace-1'", (workspace,)
            )
            sql.execute(
                "UPDATE role_bindings SET role='ws_admin',scope_id=? WHERE id='binding-1'",
                (workspace,),
            )
            sql.commit()
        get_settings().db.url = dsn
        get_settings().paths.home = tmp_path / "home"
        shared = initialize_database(DbSettings(url=dsn))
        source = tmp_path / "business.sqlite"
        sqlite3.connect(source).close()
        with shared.engine.begin() as sql:
            sql.execute(
                text(
                    "INSERT INTO datasource (tenant_id,workspace_id,datasource_id,datasource_name,datasource_type,config,is_deleted) VALUES ('tenant-1',:workspace,'warehouse','Retail','sqlite',:config,0)"
                ),
                {
                    "workspace": workspace,
                    "config": json.dumps({"path": str(source), "read_only": False}),
                },
            )
        runtime = create_context_runtime(
            graph_connection, cron_settings=CronSettings(enabled=False)
        )
        scope = runtime.issue_graph_write_scope(
            authorization(role="ws_admin", workspace_id=workspace),
            owner=WriteOwner.USER,
        )
        store = runtime.graph_store(scope)
        runtime.metadata_sync(scope).sync(scope)
        data_scope = store.access.narrow_write_scope(scope, datasource_id="warehouse")
        store.create_nodes(
            DATA_SOURCE,
            ({"datasource_name": "Retail", "datasource_type": "sqlite"},),
            scope=data_scope,
        )
        metric = store.create_nodes(
            METRIC,
            (
                {
                    "domain": "sales",
                    "name": "GMV",
                    "description": "Gross merchandise value",
                    "unit": "CNY",
                },
                {"domain": "sales", "name": "Orders", "unit": "count"},
            ),
            scope=data_scope,
        )[0]
        caliber = store.create_nodes(
            METRIC_CALIBER,
            (
                {
                    "domain": "sales",
                    "metric_name": "GMV",
                    "dataset": "sales",
                    "time_window": "1d",
                    "expr": "sum(amount)",
                },
            ),
            scope=data_scope,
        )[0]
        store.create_edges(HAS_CALIBER, ((metric, caliber, {}),), scope=data_scope)
        foreign_scope = runtime.issue_graph_write_scope(
            authorization("user-2", role="ws_admin", workspace_id=workspace),
            owner=WriteOwner.USER,
        )
        runtime.metadata_sync(foreign_scope).sync(foreign_scope)
        store.create_nodes(
            METRIC, ({"domain": "sales", "name": "Foreign owner"},), scope=foreign_scope
        )
        if backend == "neo4j":
            driver.execute_query(
                "MATCH (m:Metric {key:$metric}),(c:MetricCaliber {key:$caliber}) "
                "CREATE (m)-[:HAS {workspace_id:$workspace,owner_id:'user-2',zone:'metadata'}]->(c) "
                "SET m.embedding=$embedding, m.config='password=legacy-secret'",
                metric=metric,
                caliber=caliber,
                workspace=workspace,
                embedding=[0.1] * 1024,
                database_=database,
            )
            driver.execute_query(
                "CREATE (:Metric {key:$key,workspace_id:$workspace,owner_id:'user-1',zone:'metadata',name:'Foreign workspace'})",
                key=workspace + "-foreign",
                workspace=workspace + "-foreign",
                database_=database,
            )
        else:
            properties = dict(
                store.get_nodes(METRIC, (metric,), scope=data_scope)[0].properties
            )
            properties.update(embedding=[0.1] * 1024, config="password=legacy-secret")
            holo_cleanup.execute(
                f'UPDATE "{graphs[1]}"."Metric" SET properties=%s::ag_catalog.agtype WHERE ag_catalog.agtype_object_field_text(properties, $$key$$)=%s',
                (json.dumps(properties, default=str), metric),
            )
        app = create_api_app()
        bind_context_runtime(app, runtime)
        secret = secrets.token_hex(32)
        install_context_authentication(
            app,
            AuthConfig(secret_keys=[secret], control_plane_dsn=dsn),
            runtime.fixed_target,
        )
        token = TokenIssuer(secret).issue_access(
            user_id="user-1",
            tenant_id="tenant-1",
            active_workspace_id=workspace,
            role="member",
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=url, timeout=30) as client:
            yield LiveCM(client, url, token, dsn, backend, secret, runtime, workspace)
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=30)
            assert not thread.is_alive()
        if runtime is not None and not runtime.closed:
            asyncio.run(runtime.aclose())
        close_database()
        if driver is not None:
            try:
                driver.execute_query(
                    "MATCH (n) WHERE n.workspace_id IN $workspaces DETACH DELETE n",
                    workspaces=[workspace, workspace + "-foreign"],
                    database_=database,
                )
            finally:
                driver.close()
        if holo_cleanup is not None:
            with holo_cleanup:
                for graph in reversed(graphs):
                    if holo_cleanup.execute(
                        "SELECT 1 FROM hologres.hg_ag_graph WHERE name=%s", (graph,)
                    ).fetchone():
                        holo_cleanup.execute(
                            "CALL ag_catalog.hg_age_drop_graph(%s, true)", (graph,)
                        )


def test_real_five_tool_mcp_journey(live_cm, tmp_path):
    client, url, token, dsn, backend = (
        live_cm.client,
        live_cm.url,
        live_cm.token,
        live_cm.dsn,
        live_cm.backend,
    )

    def call(name, arguments):
        response = client.post(
            "/mcp/v1/cm",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    schema = call("get_graph_schema", {})
    assert {"version", "backend", "zones", "nodes", "edges", "query_scope"} <= set(
        schema
    )
    assert schema["backend"] == backend
    assert len(schema["nodes"]) == 14
    identity = "{workspace_id:$ws,owner_id:$owner}"
    metric_pattern = f"(m:Metric {identity})"
    caliber_pattern = f"(c:MetricCaliber {identity})"
    edge_pattern = f"[r:HAS {identity}]"
    scope_params = {"ws": schema["query_scope"]["workspace_id"], "owner": "user-1"}

    def graph_read(body, columns, params=None, limit=100):
        values = {**scope_params, **(params or {})}
        if backend == "holoage":
            projection = ", ".join(
                (
                    f"{column}::bigint"
                    if column in {"ok", "total"}
                    else f"ag_catalog.agtype_to_json({column})"
                    if column in {"metric", "properties", "items", "wrapped", "path"}
                    else f"{column}::text"
                )
                + f" AS {column}"
                for column in columns
            )
            declaration = ", ".join(f"{column} ag_catalog.agtype" for column in columns)
            statement = (
                f"SELECT {projection} FROM ag_catalog.cypher('{schema['graphs'][0]}', "
                f"$$ {body} $$, %(values)s::ag_catalog.agtype) AS result({declaration})"
            )
            values = {"values": json.dumps(values)}
        else:
            statement = body
        return call(
            "query_graph", {"cypher": statement, "params": values, "limit": limit}
        )

    assert graph_read("RETURN 1 AS ok", ["ok"])["rows"] == [{"ok": 1}]
    source = graph_read(
        f"MATCH (s:DataSource {identity}) RETURN s.datasource_id AS datasource_id",
        ["datasource_id"],
    )["rows"][0]["datasource_id"]
    metric = graph_read(
        f"MATCH {metric_pattern} WHERE m.name=$name RETURN m.name AS name,m.unit AS unit",
        ["name", "unit"],
        {"name": "GMV"},
    )
    assert metric["rows"] == [{"name": "GMV", "unit": "CNY"}]
    assert graph_read(f"MATCH {metric_pattern} RETURN count(m) AS total", ["total"])[
        "rows"
    ] == [{"total": 2}]
    # Neo4j returns a typed node. Native AGE SQL owns its explicit value projection.
    metric_value = (
        "m"
        if backend == "neo4j"
        else "{key:m.key,name:m.name,unit:m.unit,embedding:m.embedding}"
    )
    if backend == "neo4j":
        assert graph_read(
            f"MATCH {metric_pattern} WHERE m.name='GMV' "
            "WITH head(collect(m)) AS selected "
            f"MATCH (selected:Metric {identity}) RETURN selected.name AS name",
            ["name"],
        )["rows"] == [{"name": "GMV"}]
    else:
        assert call(
            "query_graph",
            {
                "cypher": (
                    "SELECT json_agg(CASE WHEN array_length(ARRAY[1,2],1)>0 "
                    "THEN ag_catalog.agtype_object_field_text(properties, 'name') ELSE '' END) AS names "
                    f'FROM "{schema["graphs"][0]}"."Metric" '
                    "WHERE ag_catalog.agtype_object_field_text(properties, 'name')=%(name)s"
                ),
                "params": {"name": "GMV"},
            },
        )["rows"] == [{"names": ["GMV"]}]
    record = graph_read(
        f"MATCH {metric_pattern} WHERE m.name='GMV' RETURN {metric_value} AS metric",
        ["metric"],
    )["rows"][0]["metric"]
    assert (
        record["name"] == "GMV" and "embedding" not in record and "config" not in record
    )
    edge = graph_read(
        f"MATCH {metric_pattern}-{edge_pattern}->{caliber_pattern} "
        "RETURN type(r) AS relationship,properties(r) AS properties,c.expr AS expression",
        ["relationship", "properties", "expression"],
    )["rows"][0]
    assert edge["relationship"] == "HAS" and edge["expression"] == "sum(amount)"
    assert edge["properties"]["owner_id"] == "user-1"
    nested = graph_read(
        f"MATCH {metric_pattern} WHERE m.name='GMV' "
        f"RETURN collect({metric_value}) AS items,{{record:{metric_value}}} AS wrapped",
        ["items", "wrapped"],
    )["rows"][0]
    assert nested["items"][0]["name"] == "GMV"
    assert nested["wrapped"]["record"]["name"] == "GMV"
    assert "config" not in json.dumps(nested) and "embedding" not in json.dumps(nested)
    path = graph_read(
        f"MATCH {metric_pattern}-{edge_pattern}->{caliber_pattern} "
        f"RETURN {{nodes:[{metric_value},properties(c)],edges:[properties(r)]}} AS path",
        ["path"],
    )["rows"][0]["path"]
    assert len(path["nodes"]) == 2 and len(path["edges"]) == 1
    assert "config" not in json.dumps(path)
    assert graph_read(f"MATCH {metric_pattern} RETURN m.key AS key", ["key"], limit=1)[
        "truncated"
    ]
    bounded = graph_read(
        f"MATCH {metric_pattern} RETURN m.key AS key ORDER BY m.key LIMIT $count",
        ["key"],
        {"count": 1},
        limit=10,
    )
    assert bounded["row_count"] == 1 and bounded["truncated"] is False
    call(
        "execute_create",
        {
            "datasource_id": source,
            "sql": "CREATE TABLE sales(id INTEGER PRIMARY KEY,amount INTEGER)",
        },
    )
    inserted = call(
        "execute_modify",
        {
            "datasource_id": source,
            "sql": "INSERT INTO sales VALUES (1,100),(2,230) RETURNING id",
            "max_rows": 1,
        },
    )
    assert inserted["row_count"] == 2 and inserted["truncated"]
    updated = call(
        "execute_modify",
        {
            "datasource_id": source,
            "sql": "UPDATE sales SET amount=amount+10 WHERE id=1",
        },
    )
    assert updated["row_count"] == 1
    result = call(
        "execute_read",
        {"datasource_id": source, "sql": "SELECT SUM(amount) AS gmv FROM sales"},
    )
    assert result["rows"] == [[340]]
    assert (
        client.get(
            result["download_url"], headers={"Authorization": f"Bearer {token}"}
        ).text.strip()
        == "gmv\r\n340"
    )

    async def host_probe():
        from agentscope.mcp import MCPClient
        from qwenpaw_data.host.core.integrations.cm import set_cm_mcp_headers
        from qwenpaw_data.host.core.integrations.cm.sql_artifact import (
            materialize_execute_sql_result,
        )
        from qwenpaw_data.host.core.settings import get_settings as host_settings
        from qwenpaw_data.host.core.utils.mcp import load_builtin_mcp_records

        settings = host_settings()
        previous = settings.cm.base_url
        settings.cm.base_url = url
        try:
            preset = load_builtin_mcp_records()["context-manager"]
            mcp = MCPClient(
                name="context-manager",
                is_stateful=False,
                mcp_config={
                    "type": "http_mcp",
                    "url": url + "/mcp/v1/cm",
                    "headers": {},
                },
                disable_tools=preset["disable_tools"],
            )
            await set_cm_mcp_headers(mcp, token)
            # Host write visibility and skill publication belong to a separate
            # integration change. This probe covers the existing read/CSV client;
            # all five CM calls above run through authenticated HTTP directly.
            assert {"get_graph_schema", "query_graph", "execute_read"} <= {
                tool.name for tool in await mcp.list_raw_tools()
            }
            tool = await mcp.get_tool("execute_read")
            result = await tool(
                datasource_id=source, sql="SELECT SUM(amount) AS gmv FROM sales"
            )
            if hasattr(result, "__aiter__"):
                result = [chunk async for chunk in result][-1]
            materialized = json.loads(
                await materialize_execute_sql_result(
                    result.content[0].text,
                    artifact_dir=tmp_path / "host",
                    access_token=token,
                )
            )
            assert "download_url" not in materialized
            assert materialized["rows"] == [[340]]
            assert Path(materialized["file_path"]).read_text().strip() == "gmv\n340"
        finally:
            settings.cm.base_url = previous

    asyncio.run(host_probe())
    # The live persisted role, not the token role hint, controls SQL writes.
    with get_db(dsn) as sql:
        sql.execute("UPDATE role_bindings SET role='member' WHERE id='binding-1'")
        sql.commit()
    response = client.post(
        "/mcp/v1/cm",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "execute_modify",
                "arguments": {
                    "datasource_id": source,
                    "sql": "UPDATE sales SET amount=0",
                },
            },
        },
    )
    assert response.json()["result"]["isError"]
    assert call(
        "execute_read",
        {"datasource_id": source, "sql": "SELECT SUM(amount) FROM sales"},
    )["rows"] == [[340]]


def test_real_native_query_rejects_cross_user_reads_and_writes(live_cm):
    def call(statement, params=None):
        response = live_cm.client.post(
            "/mcp/v1/cm",
            headers={
                "Authorization": f"Bearer {live_cm.token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "query_graph",
                    "arguments": {"cypher": statement, "params": params or {}},
                },
            },
        )
        assert response.status_code == 200
        return response.json()["result"]

    workspace = live_cm.graph_name
    if live_cm.backend == "neo4j":
        cases = [
            ("MATCH (n:Metric) RETURN n.name", {}),
            (
                "MATCH (n:Metric {workspace_id:$ws,owner_id:$owner}) RETURN n.name",
                {"ws": workspace, "owner": "user-2"},
            ),
            (
                "MATCH (n:Metric) WHERE n.workspace_id=$ws AND (n.owner_id=$owner OR true) RETURN n.name",
                {"ws": workspace, "owner": "user-1"},
            ),
            (
                "CALL db.index.fulltext.queryNodes('cm_metric_fulltext','GMV') YIELD node RETURN node",
                {},
            ),
            ("CREATE (:Metric {name:'forbidden'})", {}),
        ]
        valid = "MATCH (n:Metric {workspace_id:$ws,owner_id:$owner}) RETURN count(n) AS total"
        params = {"ws": workspace, "owner": "user-1"}
    else:
        own = f"tenant-1_{workspace}_user-1"
        foreign = f"tenant-1_{workspace}_user-2"
        cases = [
            (f'SELECT * FROM "{foreign}"."Metric"', {}),
            (
                f"SELECT * FROM ag_catalog.cypher('{foreign}', $$MATCH (n:Metric) RETURN n$$) AS (n ag_catalog.agtype)",
                {},
            ),
            (
                f"SELECT * FROM ag_catalog.cypher('{own}', $$CREATE (:Metric {{name:'forbidden'}})$$) AS (n ag_catalog.agtype)",
                {},
            ),
            (f'DELETE FROM "{own}"."Metric"', {}),
            ("SELECT 1; COMMIT; SELECT 2", {}),
        ]
        valid = f'SELECT count(*) AS total FROM "{own}"."Metric"'
        params = {}
    for statement, params_bad in cases:
        assert call(statement, params_bad).get("isError"), statement
    result = call(valid, params)
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"])["rows"] == [{"total": 2}]
