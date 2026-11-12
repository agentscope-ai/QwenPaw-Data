"""Exercise the mounted Cypher contract, authentication and native results."""

from datetime import datetime

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.runtime.startup import create_context_runtime
from fastapi import HTTPException
from fastapi.testclient import TestClient
from neo4j import Record
from neo4j.graph import Graph, Node, Path

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.test_startup import (
    FakeDriver,
    FakeSession,
    _auth_config,
    _settings,
)

URL = "/api/v1/admin/cypher"


class Result:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows
        self.fetch_count = None

    def keys(self):
        return self.columns

    def fetch(self, count):
        self.fetch_count = count
        return self.rows[:count]


class Session(FakeSession):
    def run(self, query, **params):
        self._driver.console_calls.append((query, params))
        return self._driver.result


class Driver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.result = Result(["name"], [])
        self.console_calls = []

    def session(self, **options):
        self.session_options.append(options)
        return Session(self)


@pytest.fixture
def endpoint():
    driver = Driver()
    runtime = create_context_runtime(
        _settings(), driver_factory=lambda *a, **kw: driver
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)

    def authorize(request):
        token = request.headers.get("Authorization")
        if token is None:
            raise HTTPException(401, "Not authenticated")
        role = token.removeprefix("Bearer ")
        return authorization(role=role)

    install_context_authentication(
        app, _auth_config(), runtime.fixed_target, authorizer=authorize
    )
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer member"
    try:
        yield client, driver
    finally:
        client.close()
        runtime.close()
    assert driver.close_count == int(bool(driver.session_options))


def native_graph():
    graph = Graph()
    a = Node(
        graph,
        "node-a",
        1,
        ["Metric"],
        {
            "key": "a",
            "name": "Revenue",
            "zone": "metadata",
            "datasource_id": "sales",
            "embedding": [1.0],
            "embedding_hash": "hash",
            "signature_emb": [1.0],
        },
    )
    b = Node(
        graph,
        "node-b",
        2,
        ["Dimension"],
        {
            "key": "b",
            "name": "City",
            "zone": "metadata",
            "datasource_id": "elsewhere",
        },
    )
    edge = graph.relationship_type("RELATED_TO")(
        graph,
        "edge-ab",
        1,
        {
            "weight": 1,
            "embedding": [1.0],
            "embedding_hash": "hash",
            "signature_emb": [2.0],
        },
    )
    edge._start_node, edge._end_node = a, b
    return a, b, edge, Path(a, edge)


def test_relationship_path_lists_and_serialized_table_rows(endpoint):
    client, driver = endpoint
    a, b, edge, path = native_graph()
    record = Record(
        {"r": edge, "p": path, "nested": [[a, b, edge]], "date": datetime(2026, 10, 3)}  # noqa: DTZ001 - intentional test fixture
    )
    driver.result = Result(list(record.keys()), [record])
    result = client.post(
        URL, json={"cypher": "MATCH p=(a)-[r]->(b) RETURN r,p,[a,b] AS nested"}
    ).json()["data"]
    assert len(result["graph"]["nodes"]) == 2
    assert result["graph"]["edges"] == [
        {
            "source_key": "a",
            "target_key": "b",
            "rel_type": "RELATED_TO",
            "properties": {"weight": 1, "signature_emb": [2.0]},
        }
    ]
    assert result["rows"][0]["date"] == "2026-10-03T00:00:00"
    assert result["rows"][0]["r"][1] == "RELATED_TO"
