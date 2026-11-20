"""Real Auth, Runtime and Neo4j checks for the Explorer Cypher console.

CM_EXPLORER_TEST_NEO4J_URI must point to a disposable local test instance.
"""

import os
from urllib.parse import urlparse

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.db import initialize_database
from context_manager.runtime import Neo4jConnectionConfig
from context_manager.runtime.startup import create_context_runtime
from context_manager.settings.db import DbSettings
from qwenpaw_data_auth.database import get_db
from qwenpaw_data_auth.token import TokenIssuer
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from cm.infrastructure.lifecycle.test_startup import _auth_config, _seed_member

URL = "/api/v1/admin/cypher"


@pytest.fixture
def live_console(tmp_path, monkeypatch):
    uri = os.environ.get("CM_EXPLORER_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("set CM_EXPLORER_TEST_NEO4J_URI to an isolated Neo4j")
    assert urlparse(uri).hostname in {"localhost", "127.0.0.1", "::1"}
    password = os.environ["CM_EXPLORER_TEST_NEO4J_PASSWORD"]
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "qwenpaw-data"))
    dsn = f"sqlite:///{tmp_path / 'auth.sqlite'}"
    _seed_member(dsn)
    auth_config = _auth_config(dsn)
    initialize_database(DbSettings(url=dsn))
    settings = Neo4jConnectionConfig(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        neo4j_uri=uri,
        neo4j_user="neo4j",
        neo4j_password=password,
        neo4j_database="neo4j",
    )
    runtime = create_context_runtime(settings)
    app = create_api_app()
    bind_context_runtime(app, runtime)
    install_context_authentication(app, auth_config, runtime.fixed_target)
    client = TestClient(app)
    token = TokenIssuer("test-auth-secret").issue_access(
        user_id="user-1",
        tenant_id="tenant-1",
        active_workspace_id="workspace-1",
        role="member",
    )
    client.headers["Authorization"] = f"Bearer {token}"
    driver = GraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        with driver.session(database="neo4j") as session:
            session.run(
                """
                UNWIND $rows AS row
                CREATE (n:CypherConsoleFixture) SET n = row
            """,
                rows=[
                    {
                        "key": "own",
                        "workspace_id": "workspace-1",
                        "owner_id": "user-1",
                        "name": "own",
                    },
                    {
                        "key": "global",
                        "workspace_id": "workspace-1",
                        "owner_id": "__global__",
                        "name": "shared",
                    },
                    {
                        "key": "other",
                        "workspace_id": "workspace-1",
                        "owner_id": "user-2",
                        "name": "other",
                    },
                    {
                        "key": "foreign",
                        "workspace_id": "workspace-2",
                        "owner_id": "user-1",
                        "name": "foreign",
                    },
                ],
            ).consume()
            session.run("""
                MATCH (a:CypherConsoleFixture {key:'own'}), (b:CypherConsoleFixture {key:'global'})
                CREATE (a)-[:CONSOLE_REL {workspace_id:'workspace-1',owner_id:'user-1'}]->(b)
                CREATE (a)-[:CONSOLE_REL {workspace_id:'workspace-2',owner_id:'user-1'}]->(b)
                CREATE (a)-[:CONSOLE_REL {workspace_id:'workspace-1',owner_id:'user-2'}]->(b)
            """).consume()
        yield client, driver, dsn
    finally:
        with driver.session(database="neo4j") as session:
            session.run("MATCH (n:CypherConsoleFixture) DETACH DELETE n").consume()
        client.close()
        runtime.close()
        driver.close()


@pytest.mark.parametrize("role", ["member", "ws_admin", "tenant_admin"])
def test_real_auth_scope_graph_and_read_only(live_console, role):
    client, driver, dsn = live_console
    # Trusted authorization reads the persisted role, not the token role hint.
    with get_db(dsn) as connection:
        connection.execute(
            "UPDATE role_bindings SET role=?, scope_type=?, scope_id=? WHERE id=?",
            (
                role,
                "tenant" if role == "tenant_admin" else "workspace",
                "tenant-1" if role == "tenant_admin" else "workspace-1",
                "binding-1",
            ),
        )
        connection.commit()
    result = client.post(
        URL,
        json={
            "cypher": "MATCH (n:CypherConsoleFixture) RETURN n ORDER BY n.key",
        },
    )
    assert result.status_code == 200, result.text
    expected = {"own", "global"} if role == "member" else {"own", "global", "other"}
    assert {node["key"] for node in result.json()["data"]["graph"]["nodes"]} == expected

    result = client.post(
        URL,
        json={
            "cypher": "MATCH (n:CypherConsoleFixture) RETURN n ORDER BY n.key",
            "scope": "global_mg",
        },
    ).json()["data"]
    assert {node["key"] for node in result["graph"]["nodes"]} == (
        expected if role == "member" else {"global"}
    )

    result = client.post(
        URL,
        json={
            "cypher": "MATCH p=(a:CypherConsoleFixture)-[r:CONSOLE_REL]->(b:CypherConsoleFixture) RETURN p,r",
        },
    ).json()["data"]
    assert result["count"] == (1 if role == "member" else 2)
    assert len(result["graph"]["nodes"]) == 2
    # Develop deduplicates parallel edges by endpoint keys and relationship type.
    assert len(result["graph"]["edges"]) == 1

    result = client.post(
        URL,
        json={
            "cypher": "MATCH (n:CypherConsoleFixture) WHERE n.key=$key RETURN n",
            "params": {
                "key": "foreign",
                "scoped_ws_id": "workspace-2",
                "scoped_uid": "user-2",
            },
        },
    ).json()["data"]
    assert result["count"] == 0 and result["columns"] == ["n"]

    assert (
        client.post(
            URL, json={"cypher": "MATCH (n:CypherConsoleFixture) DETACH DELETE n"}
        ).status_code
        == 403
    )
    with driver.session(database="neo4j") as session:
        assert (
            session.run("MATCH (n:CypherConsoleFixture) RETURN count(n) AS n").single()[
                "n"
            ]
            == 4
        )

    client.headers.pop("Authorization")
    assert client.post(URL, json={"cypher": "MATCH (n) RETURN n"}).status_code == 401
