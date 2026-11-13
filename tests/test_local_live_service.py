"""Opt-in, real-process local service and Host-to-Context acceptance check.

Set CM_MCP_NEO4J_URI and CM_MCP_NEO4J_PASSWORD to a disposable loopback
Neo4j instance. The test creates isolated SQLite databases and child services;
the graph server must also be dedicated to this test.
"""

import asyncio
import json
import os
import select
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import openpyxl
import pytest


@pytest.mark.skipif(
    not os.getenv("CM_MCP_NEO4J_URI"), reason="requires disposable local Neo4j"
)
@pytest.mark.parametrize("block_optional_drivers", [False, True])
def test_local_services_host_mcp_and_sql_artifact(tmp_path, monkeypatch, block_optional_drivers):
    from agentscope.mcp import MCPClient
    from qwenpaw_data.host.core.integrations.cm.sql_artifact import (
        materialize_execute_sql_result,
    )
    from qwenpaw_data.host.core.settings import get_settings

    uri = os.environ["CM_MCP_NEO4J_URI"]
    assert urlparse(uri).hostname in {"localhost", "127.0.0.1", "::1"}
    password = os.environ["CM_MCP_NEO4J_PASSWORD"]
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme != "bolt":
        pytest.skip("outage proxy requires a direct unencrypted Bolt URI")
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("TRACING_ENABLED", "0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith((
            "QWENPAW_DATA_", "NEO4J_", "HOLO_AGE_", "CM_GRAPH_", "TRACING_", "OTEL_",
        )):
            env.pop(key)
    assert "TRACING_ENABLED" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    env.update(
        QWENPAW_DATA_HOME=str(tmp_path / "home"),
        QWENPAW_DATA_ENV_FILE=str(tmp_path / "absent.env"),
        QWENPAW_DATA_AUTH_MODE="local",
        QWENPAW_DATA_WORKSPACE_BACKEND="local",
        QWENPAW_DATA_DB_URL=f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}",
        GRAPH_BACKEND="neo4j",
        NEO4J_URI=uri,
        NEO4J_BOLT_PORT=str(urlparse(uri).port or 7687),
        NEO4J_USER="neo4j",
        NEO4J_PASSWORD=password,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(filter(None, [
            os.getenv("QPD_TEST_NEO4J_DRIVER_PATH", ""),
            *(str(path) for path in root.glob("packages/*/src")),
        ])),
    )
    children = []
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 1\n")
    fake_docker.chmod(0o755)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["CONTEXT_PYTHON"] = sys.executable

    unavailable_graph = threading.Event()

    class ProxyHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                if unavailable_graph.is_set():
                    return
                with socket.create_connection((parsed_uri.hostname, parsed_uri.port or 7687), timeout=3) as upstream:
                    upstream.settimeout(None)
                    peers = {self.request: upstream, upstream: self.request}
                    while not unavailable_graph.is_set():
                        ready, _, _ = select.select(list(peers), [], [], 0.05)
                        for connection in ready:
                            data = connection.recv(65536)
                            if not data:
                                return
                            peers[connection].sendall(data)
            except OSError:
                return

    class ProxyServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    proxy = ProxyServer(("127.0.0.1", 0), ProxyHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05})
    proxy_thread.start()
    env["NEO4J_URI"] = f"bolt://127.0.0.1:{proxy.server_address[1]}"

    class EmbeddingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            inputs = request["input"]
            if isinstance(inputs, str):
                inputs = [inputs]
            response = json.dumps({
                "object": "list", "model": request["model"],
                "data": [{"object": "embedding", "index": i, "embedding": [0.01] * 1024}
                         for i in range(len(inputs))],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    embedding_server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    embedding_thread = threading.Thread(target=embedding_server.serve_forever, kwargs={"poll_interval": 0.05})
    embedding_thread.start()

    def start(label, module, health_path, *, expected_status=200, child_env=None):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = (tmp_path / f"{label}.log").open("w+")
        args = [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)]
        if label == "context" and not block_optional_drivers:
            args = ["bash", str(root / "scripts/start_databridge.sh"), "--skip-frontend", "--skip-neo4j", "--port", str(port)]
        elif label == "context":
            args = [sys.executable, "-c", """
import importlib.abc, sys
class BlockOptionalDrivers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('psycopg', 'psycopg_pool', 'oss2', 'odps', 'jaydebeapi',
                   'dbt', 'google.cloud.bigquery', 'alibabacloud_oss_v2')
        if any(fullname == item or fullname.startswith(item + '.') for item in blocked):
            raise ModuleNotFoundError('Optional driver intentionally unavailable', name=fullname)
sys.meta_path.insert(0, BlockOptionalDrivers())
import uvicorn
uvicorn.run(sys.argv[1], host='127.0.0.1', port=int(sys.argv[2]))
""", module, str(port)]
        process = subprocess.Popen(
            args,
            cwd=tmp_path,
            env=child_env or env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        children.append((process, log))
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 45
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if client.get(url + health_path).status_code == expected_status:
                        return url
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
        log.flush()
        log.seek(0)
        pytest.fail(f"{label} did not become ready:\n{log.read()}")

    try:
        host_url = start("host", "qwenpaw_data.host.core.api.app:app", "/api/auth/status")
        start("auth", "qwenpaw_data_auth.server:app", "/api/health")
        url = start("context", "context_manager.server:app", "/api/health")
        unpublished_name = "No model blocked" if block_optional_drivers else "No model normal"
        unpublished = subprocess.run(
            [sys.executable, "-B", "-m", "qwenpaw_data.cli.main", "semantic", "domain", "create",
             "--name", unpublished_name, "--description", "Saved before model configuration"],
            cwd=tmp_path, env={**env, "QWENPAW_DATA_CM_BASE_URL": url},
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert unpublished.returncode == 1
        assert "配置已保存" in unpublished.stderr
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
            saved = client.get("/api/semantic-config/biz-domain", params={"domain_name": unpublished_name})
            assert saved.status_code == 200 and saved.json()["total"] == 1
        from neo4j import GraphDatabase

        def graph_domain_count():
            with GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
                with driver.session() as session:
                    return session.run(
                        "MATCH (d:Domain {name: $name}) RETURN count(d) AS n", name=unpublished_name
                    ).single()["n"]

        assert graph_domain_count() == 0
        with httpx.Client(base_url=host_url, trust_env=False, timeout=10) as client:
            provider = client.put(
                "/api/v1/preferences/providers/openai",
                json={"api_key": "local-test-key", "base_url": f"http://127.0.0.1:{embedding_server.server_port}/v1"},
            )
            assert provider.status_code == 200, provider.text
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
            embedding = client.put(
                "/api/v1/config/embed",
                json={"provider_id": "openai", "model": "local-test-embed", "dim": 1024},
            )
            assert embedding.status_code == 200, embedding.text
        invalid_url = start(
            "context-invalid-database",
            "context_manager.server:app",
            "/api/health",
            expected_status=503,
            child_env={**env, "NEO4J_DATABASE": "qpd_nonexistent_database"},
        )
        with httpx.Client(base_url=invalid_url, trust_env=False, timeout=5) as client:
            assert client.get("/api/health").status_code == 503
        source = tmp_path / "business.sqlite"
        with sqlite3.connect(source) as db:
            db.executescript(
                "CREATE TABLE sales (amount INTEGER); INSERT INTO sales VALUES (100), (240);"
            )
        with httpx.Client(base_url=url, trust_env=False, timeout=20) as client:
            response = client.post(
                "/api/semantic-config/datasource",
                json={"datasource_name": "Retail", "datasource_type": "sqlite", "config": {"path": str(source)}},
            )
            response.raise_for_status()
            datasource = response.json()["datasource_id"]
            with sqlite3.connect(tmp_path / "shared.db") as db:
                db.executemany(
                    "INSERT INTO datasource (tenant_id, workspace_id, datasource_id, "
                    "datasource_name, datasource_type, config, is_deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    [
                        ("foreign-tenant", "default", "foreign-tenant-source", "Foreign", "sqlite", json.dumps({"path": str(source), "password": "private"})),
                        ("default", "foreign-workspace", "foreign-workspace-source", "Foreign", "sqlite", json.dumps({"path": str(source), "password": "private"})),
                    ],
                )
            metadata = client.get("/api/v1/cm/datasources", params={"page": 1, "size": 100})
            assert metadata.status_code == 200
            assert metadata.json()["total"] == 1
            assert metadata.json()["records"] == [{
                "datasource_id": datasource,
                "datasource_name": "Retail",
                "datasource_type": "sqlite",
            }]
            extra = client.post(
                "/api/semantic-config/datasource",
                json={"datasource_name": "Second", "datasource_type": "sqlite", "config": {"path": str(source)}},
            )
            extra.raise_for_status()
            extra_id = extra.json()["datasource_id"]
            pages = [client.get("/api/v1/cm/datasources", params={"page": page, "size": 1}) for page in (1, 2)]
            assert all(page.status_code == 200 and page.json()["total"] == 2 for page in pages)
            assert {page.json()["records"][0]["datasource_id"] for page in pages} == {datasource, extra_id}
            for params in ({"page": 0}, {"size": 0}, {"size": 501}):
                assert client.get("/api/v1/cm/datasources", params=params).status_code == 422
            deleted = client.delete(f"/api/semantic-config/datasource/{extra_id}")
            assert deleted.status_code == 200
            after_delete = client.get("/api/v1/cm/datasources", params={"page": 1, "size": 1})
            assert after_delete.status_code == 200
            assert after_delete.json()["total"] == 1
            assert after_delete.json()["records"][0]["datasource_id"] == datasource
            assert extra_id not in after_delete.text
            assert client.get("/api/v1/cm/datasources", params={"page": 2, "size": 1}).json()["records"] == []
            bad_token = client.post(
                "/mcp/v1/cm",
                headers={"Authorization": "Bearer invalid-token", "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_read",
                        "arguments": {"datasource_id": datasource, "sql": "SELECT 1"},
                    },
                },
            )
            assert bad_token.status_code == 401
            postgres_source = None
            if block_optional_drivers:
                response = client.post(
                    "/api/semantic-config/datasource",
                    json={
                        "datasource_name": "Unavailable PostgreSQL",
                        "datasource_type": "postgresql",
                        "config": {
                            "host": "127.0.0.1",
                            "port": 1,
                            "database": "test",
                            "user": "test",
                            "password": "test-password-not-for-logging",
                        },
                    },
                )
                response.raise_for_status()
                postgres_source = response.json()["datasource_id"]

        read_key = "qpd-read-test-0123456789"
        write_key = "qpd-write-test-0123456789"
        credential_key = "qpd-credentials-test-0123456789"
        scoped_url = start(
            "context-scoped-keys", "context_manager.server:app", "/api/health",
            child_env={**env, "QWENPAW_DATA_API_KEYS": json.dumps([
                {"id": "read", "token": read_key, "scopes": ["query"]},
                {"id": "write", "token": write_key, "scopes": ["write"]},
                {"id": "credentials", "token": credential_key, "scopes": ["credentials:manage"]},
            ])},
        )
        with httpx.Client(base_url=scoped_url, trust_env=False, timeout=5) as client:
            assert client.get("/api/v1/cm/datasources").status_code == 401
            assert client.get(
                "/api/v1/cm/datasources", headers={"Authorization": f"Bearer {write_key}"}
            ).status_code == 403
            response = client.get(
                "/api/v1/cm/datasources", headers={"Authorization": f"Bearer {read_key}"}
            )
            assert response.status_code == 200
            assert datasource in {row["datasource_id"] for row in response.json()["records"]}
            assert "foreign-tenant-source" not in response.text
            assert "foreign-workspace-source" not in response.text
            assert all(set(row) == {"datasource_id", "datasource_name", "datasource_type"}
                       for row in response.json()["records"])
            def execute_with_key(key, source_id):
                return client.post(
                    "/mcp/v1/cm",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "execute_read", "arguments": {
                            "datasource_id": source_id, "sql": "SELECT SUM(amount) FROM sales",
                        }},
                    },
                )

            own = execute_with_key(read_key, datasource)
            assert own.status_code == 200
            assert own.json()["result"]["isError"] is False
            assert json.loads(own.json()["result"]["content"][0]["text"])["rows"] == [[340]]
            for source_id in ("foreign-tenant-source", "foreign-workspace-source"):
                denied = execute_with_key(read_key, source_id)
                assert denied.status_code == 200
                assert denied.json()["result"]["isError"] is True
                assert "private" not in denied.text
                assert "340" not in denied.text
            denied_scope = execute_with_key(write_key, datasource)
            assert denied_scope.status_code in (200, 403)
            if denied_scope.status_code == 200:
                assert denied_scope.json()["result"]["isError"] is True
            assert "340" not in denied_scope.text

        from fastapi.testclient import TestClient
        from qwenpaw_data.console import create_app as create_console

        portal_assets = tmp_path / "portal-assets"
        portal_assets.mkdir()
        (portal_assets / "index.html").write_text("<!doctype html><title>Test</title>")
        for key, expected in ((credential_key, 200), (read_key, 403)):
            portal = create_console(
                host_url=host_url, token="unused-host-token", frontend=portal_assets,
                bridge_url=scoped_url, bridge_token=key,
            )
            with TestClient(portal) as client:
                url_path = "/databridge/api/system/model-config/"
                assert client.get(url_path).status_code == 403
                response = client.get(url_path, headers={"X-QwenPaw-Console": "1"})
                assert response.status_code == expected, response.text
                assert key not in response.text
                assert "local-test-key" not in response.text
                if expected == 200:
                    assert response.json()["configured"] is False

        cli = subprocess.run(
            [sys.executable, "-B", "-m", "qwenpaw_data.cli.main", "datasource", "list"],
            cwd=tmp_path,
            env={**env, "QWENPAW_DATA_CM_BASE_URL": url},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert cli.returncode == 0, cli.stderr
        assert datasource in {item["datasource_id"] for item in json.loads(cli.stdout)["items"]}
        doctor = subprocess.run(
            [sys.executable, "-B", "-m", "qwenpaw_data.cli.main", "doctor", "--json"],
            cwd=tmp_path, env={**env, "QWENPAW_DATA_CM_BASE_URL": url},
            capture_output=True, text=True, timeout=30, check=False,
        )
        checks = {check["name"]: check for check in json.loads(doctor.stdout)["checks"]}
        graph_check = checks["Neo4j (bolt)"]
        assert graph_check["status"] == "OK"
        assert f"127.0.0.1:{proxy.server_address[1]}" in graph_check["detail"]
        assert "authentication and database not checked" in graph_check["detail"]
        assert checks["Model config"]["status"] == "WARN"
        assert "embedding configured; chat model is not configured" in checks["Model config"]["detail"]
        assert password not in doctor.stdout
        assert "local-test-key" not in doctor.stdout

        def semantic_cli(*args):
            result = subprocess.run(
                [sys.executable, "-B", "-m", "qwenpaw_data.cli.main", "semantic", *args],
                cwd=tmp_path,
                env={**env, "QWENPAW_DATA_CM_BASE_URL": url},
                capture_output=True, text=True, timeout=45, check=False,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        domain = semantic_cli("domain", "create", "--name", "Retail", "--description", "Sales domain")
        domain_id = str(domain["domain_id"])
        dataset = semantic_cli(
            "dataset", "create", "--datasource-id", datasource, "--domain-id", domain_id,
            "--name", "Sales", "--description", "Sales data", "--source", "sales",
        )
        dataset_id = str(dataset["dataset_id"])
        column = semantic_cli("column", "create", "--dataset-id", dataset_id, "--name", "amount", "--data-type", "INTEGER")
        assert column["field_name"] == "amount"
        dimension = semantic_cli("dimension", "create", "--domain-id", domain_id, "--name", "Region")
        metric = semantic_cli("metric", "create", "--domain-id", domain_id, "--name", "Revenue")
        metric_id = str(metric["metric_id"])
        semantic_cli("binding", "create", "--dataset-id", dataset_id, "--dimension-id", str(dimension["dimension_id"]), "--expr", "region")
        semantic_cli("formula", "create", "--dataset-id", dataset_id, "--metric-id", metric_id, "--expr", "sum(amount)", "--time-window", "daily")
        assert semantic_cli("formula", "list", "--metric-id", metric_id)["total"] == 1
        semantic_cli("formula", "delete", "--metric-id", metric_id, "--yes")
        assert semantic_cli("formula", "list", "--metric-id", metric_id)["total"] == 0
        semantic_cli("binding", "delete", "--dataset-id", dataset_id, "--yes")
        assert semantic_cli("binding", "list", "--dataset-id", dataset_id)["total"] == 0
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
            template = client.get("/api/semantic-config/import/excel/template", params={"object": "biz_domain"})
            assert template.status_code == 200
        workbook_path = tmp_path / "semantic-import.xlsx"
        workbook_path.write_bytes(template.content)
        workbook = openpyxl.load_workbook(workbook_path)
        workbook["biz_domain"].append(["Imported domain", "Imported description"])
        workbook.save(workbook_path)
        imported = semantic_cli("import", "--file", str(workbook_path), "--datasource-id", datasource)
        assert imported["success"] is True
        assert semantic_cli("domain", "list", "--name", "Imported domain")["total"] == 1
        workbook["biz_domain"].append(["Invalid domain", None])
        workbook.save(workbook_path)
        workbook.close()
        invalid = subprocess.run(
            [sys.executable, "-B", "-m", "qwenpaw_data.cli.main", "semantic", "import",
             "--file", str(workbook_path), "--datasource-id", datasource],
            cwd=tmp_path, env={**env, "QWENPAW_DATA_CM_BASE_URL": url},
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert invalid.returncode == 1
        assert json.loads(invalid.stdout)["success"] is False
        assert semantic_cli("domain", "list", "--name", "Invalid domain")["total"] == 0
        weave = semantic_cli("weave", "submit", "--datasource-id", datasource, "--name", "Retail weave", "--wait", "--timeout", "30")
        assert weave["status"] == "SUCCESS"
        assert graph_domain_count() == 1

        monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "local")
        monkeypatch.setattr(get_settings().cm, "base_url", url)

        async def host_journey():
            mcp = MCPClient(
                name="context-manager",
                is_stateful=False,
                mcp_config={"type": "http_mcp", "url": url + "/mcp/v1/cm", "headers": {}},
            )
            assert {"execute_read", "get_graph_schema", "query_graph"} <= {
                tool.name for tool in await mcp.list_raw_tools()
            }
            schema_tool = await mcp.get_tool("get_graph_schema")
            schema = await schema_tool()
            if hasattr(schema, "__aiter__"):
                schema = [chunk async for chunk in schema][-1]
            assert json.loads(schema.content[0].text)["backend"] == "neo4j"
            graph_tool = await mcp.get_tool("query_graph")
            graph = await graph_tool(cypher="MATCH (m:Metric) RETURN count(m) AS total")
            if hasattr(graph, "__aiter__"):
                graph = [chunk async for chunk in graph][-1]
            assert json.loads(graph.content[0].text)["rows"] == [{"total": 0}]
            tool = await mcp.get_tool("execute_read")
            result = await tool(datasource_id=datasource, sql="SELECT SUM(amount) AS total FROM sales")
            if hasattr(result, "__aiter__"):
                result = [chunk async for chunk in result][-1]
            payload = json.loads(await materialize_execute_sql_result(
                result.content[0].text, artifact_dir=tmp_path / "artifacts", access_token=None
            ))
            assert payload["rows"] == [[340]]
            assert "download_url" not in payload
            assert Path(payload["file_path"]).read_text().strip() == "total\n340"
            rejected = await tool(datasource_id=datasource, sql="DELETE FROM sales")
            if hasattr(rejected, "__aiter__"):
                rejected = [chunk async for chunk in rejected][-1]
            assert "read-only" in rejected.content[0].text.lower()
            deleted_source = await tool(datasource_id=extra_id, sql="SELECT 1")
            if hasattr(deleted_source, "__aiter__"):
                deleted_source = [chunk async for chunk in deleted_source][-1]
            assert "datasource is unavailable" in deleted_source.content[0].text.lower()
            if postgres_source:
                unavailable = await tool(datasource_id=postgres_source, sql="SELECT 1")
                if hasattr(unavailable, "__aiter__"):
                    unavailable = [chunk async for chunk in unavailable][-1]
                assert "driver psycopg is not installed" in unavailable.content[0].text
                assert "test-password-not-for-logging" not in unavailable.content[0].text

        asyncio.run(host_journey())
        with sqlite3.connect(source) as db:
            assert db.execute("SELECT COUNT(*) FROM sales").fetchone() == (2,)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
            assert client.get("/api/health").status_code == 200
            unavailable_graph.set()
            time.sleep(0.15)
            assert client.get("/api/health").status_code == 503
            response = client.post(
                "/mcp/v1/cm",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {
                        "name": "query_graph",
                        "arguments": {"cypher": "MATCH (m:Metric) RETURN count(m) AS total"},
                    },
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is True
            unavailable_graph.clear()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if client.get("/api/health").status_code == 200:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Context readiness did not recover after graph connectivity returned")
            recovered = client.post(
                "/mcp/v1/cm",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {
                        "name": "query_graph",
                        "arguments": {"cypher": "MATCH (m:Metric) RETURN count(m) AS total"},
                    },
                },
            )
            assert recovered.status_code == 200
            result = recovered.json()["result"]
            assert result["isError"] is False
            assert json.loads(result["content"][0]["text"])["rows"] == [{"total": 0}]
    finally:
        unavailable_graph.clear()
        for process, _ in reversed(children):
            process.terminate()
        for process, log in reversed(children):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        embedding_server.shutdown()
        embedding_server.server_close()
        embedding_thread.join(timeout=5)
