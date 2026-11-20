"""Legacy Graph Center journeys use the selected authorized graph."""

import os
from functools import partial
from uuid import uuid4

import pytest
from context_manager.api.app import create_api_app
from context_manager.api.dependencies import (
    require_context_authorization,
    require_context_runtime,
)
from context_manager.auth import GraphScopeAuthority, WriteOwner
from context_manager.metadata import MetadataSyncService
from context_manager.schema import (
    DOMAIN,
    ENTITY,
    GRAPH_SCHEMA,
    HAS_METRIC,
    METRIC,
    USER_MEMORY,
    USER_MEMORY_REFERS_TO_ENTITY,
    USER_MEMORY_REFERS_TO_METRIC,
)
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from cm.infrastructure.access.support import authorization as _authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.metadata.test_sync import create, seed_metadata

_NAMESPACE = "explorer-test-" + uuid4().hex
WORKSPACE = _NAMESPACE
ALICE, BOB, ADMIN = (_NAMESPACE + "-" + name for name in ("alice", "bob", "admin"))
authorization = partial(_authorization, workspace_id=WORKSPACE)

BASE = "/api/v1/admin/explorer"


@pytest.fixture
def app_graph(monkeypatch, request, database_engine):
    if os.environ.get("CM_RUN_LIVE_GRAPH_TESTS") != "1":
        pytest.skip("set CM_RUN_LIVE_GRAPH_TESTS=1 for Neo4j-backed Explorer journeys")
    driver = GraphDatabase.driver(
        os.environ["CM_LIVE_NEO4J_URI"],
        auth=("neo4j", os.environ["CM_LIVE_NEO4J_PASSWORD"]),
    )

    def cleanup():
        with driver.session() as session:
            session.run(
                "MATCH (n {workspace_id: $workspace}) DETACH DELETE n",
                workspace=WORKSPACE,
            ).consume()
        driver.close()

    request.addfinalizer(cleanup)
    physical = Neo4jRuntime(driver, GraphTarget("tenant-1", WORKSPACE))
    store = Neo4jGraphStore(
        physical, GRAPH_SCHEMA, GraphScopeAuthority(), make_glossary(GRAPH_SCHEMA)
    )
    shared = store.access.issue_write_scope(
        authorization(ADMIN, role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        zone="metadata",
    )
    alice = store.access.issue_write_scope(authorization(ALICE), owner=WriteOwner.USER)
    bob = store.access.issue_write_scope(authorization(BOB), owner=WriteOwner.USER)
    from context_manager import schema

    for datasource in ("east", "west"):
        nodes = seed_metadata(store, shared, datasource, embedding_dim=1024)
        graph = {n.spec.name: n for n in nodes}
        for source, rel, target in (
            ("Domain", "HAS", "Dataset"),
            ("Domain", "HAS", "Dimension"),
            ("Metric", "HAS", "MetricCaliber"),
            ("MetricCaliber", "CALIBER_ON", "Dataset"),
            ("Dataset", "HAS", "DatasetField"),
            ("Dimension", "MAPS_TO_FIELD", "DatasetField"),
            ("VerifiedQuery", "ANCHORED_TO", "Domain"),
            ("DataSource", "HAS", "Table"),
            ("Table", "HAS", "Column"),
        ):
            spec = next(
                e
                for e in schema.GRAPH_SCHEMA.edges
                if (e.source.name, e.relationship, e.target.name)
                == (source, rel, target)
            )
            props = {"expr": "amount"} if rel == "MAPS_TO_FIELD" else {}
            store.create_edges(
                spec,
                ((graph[source].key, graph[target].key, props),),
                scope=store.access.narrow_write_scope(shared, datasource_id=datasource),
            )
    sync = MetadataSyncService(store, database_engine=database_engine)
    sync.sync(alice)
    sync.sync(bob)
    personal = create(store, alice, DOMAIN, domain_id="private", name="Alice private")
    entity = create(
        store,
        alice,
        ENTITY,
        identity_hash="sales-concept",
        name="Sales concept",
        type="product",
        description="Business sales",
    )
    runtime = bound_context_runtime(
        store, RdbConnector(), make_glossary(store.schema), workspace_id=WORKSPACE
    )
    identity = {"value": authorization(ALICE)}
    app = create_api_app()
    app.dependency_overrides[require_context_runtime] = lambda: runtime
    app.dependency_overrides[require_context_authorization] = lambda: identity["value"]
    yield (
        TestClient(app),
        store,
        runtime,
        identity,
        shared,
        alice,
        bob,
        personal,
        entity,
    )


def test_domain_expand_search_details_and_counts_describe_persisted_personal_facts(
    app_graph,
):
    client, store, _, _, _, alice, _, personal, _ = app_graph
    metric = create(
        store,
        alice,
        METRIC,
        domain="private",
        name="Private margin",
        description="Alice net revenue",
    )
    store.create_edges(HAS_METRIC, ((personal.key, metric.key, {}),), scope=alice)
    snapshot = client.post(
        BASE + "/global-graph", json={"zone_mode": "metadata", "skeleton": False}
    )
    assert snapshot.status_code == 200
    assert metric.key in {n["id"] for n in snapshot.json()["data"]["nodes"]}
    graph = client.post(
        BASE + "/domain-graph", json={"domain": "Alice private"}
    ).json()["data"]
    assert graph["center"] == personal.key
    assert {n["id"] for n in graph["nodes"]} == {personal.key, metric.key}
    for path, body in [
        ("/expand-node", {"key": personal.key}),
        ("/expand-layer", {"key": personal.key, "direction": "out"}),
        ("/expand-layer", {"key": metric.key, "direction": "up"}),
    ]:
        data = client.post(BASE + path, json=body).json()["data"]
        assert {n["id"] for n in data["nodes"]} == {personal.key, metric.key}
        assert data["edges"][0]["type"] == "HAS"
    # develop's Neo4j OPTIONAL MATCH returns a null-neighbor row at a leaf.
    leaf = client.post(
        BASE + "/expand-layer", json={"key": metric.key, "direction": "down"}
    ).json()["data"]
    assert [n["id"] for n in leaf["nodes"]] == [metric.key]
    assert leaf["edges"] == []
    hits = client.post(BASE + "/search-nodes", json={"query": "Private margin"}).json()[
        "data"
    ]
    assert hits[0]["id"] == metric.key and hits[0]["score"] == 100
    subgraph = client.post(
        BASE + "/search-subgraph", json={"query": "Alice net", "hops": 2}
    ).json()["data"]
    assert subgraph["hit_nodes"][0]["key"] == metric.key
    assert {n["key"] for n in subgraph["nodes"]} == {personal.key, metric.key}
    exact = client.post(
        BASE + "/search-subgraph", json={"query": "Alice net", "match_mode": "exact"}
    ).json()["data"]
    assert exact["hit_nodes"] == []
    detail = client.get(BASE + "/nodes/" + metric.key).json()["data"]
    assert detail["properties"]["description"] == "Alice net revenue"
    assert "embedding" not in detail["properties"]
    assert detail["editable_fields"] == []
    edge = client.post(
        BASE + "/edge-detail",
        json={
            "source_key": personal.key,
            "target_key": metric.key,
            "rel_type": "HAS",
        },
    )
    assert edge.status_code == 200
    assert edge.json()["data"]["is_cross_graph"] is False
    schema = client.get(BASE + "/schema").json()["data"]
    assert (
        next(n["count"] for n in schema["node_labels"] if n["label"] == "Metric") == 3
    )
    missing = client.get(BASE + "/nodes/missing")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert (
        client.get(BASE + "/nodes/" + metric.key + "/cross-graph").json()["data"] == []
    )


def test_cross_graph_links_and_relationship_counts_keep_the_selected_owner(app_graph):
    client, store, _, identity, _, alice, _, _, entity = app_graph
    memory = create(
        store,
        alice,
        USER_MEMORY,
        id="alice-revenue",
        type="preference",
        description="收入分析优先看净收入",
        status="active",
    )
    metric = store.list_nodes(METRIC, scope=alice)[0]
    store.create_edges(
        USER_MEMORY_REFERS_TO_METRIC, ((memory.key, metric.key, {}),), scope=alice
    )
    store.create_edges(
        USER_MEMORY_REFERS_TO_ENTITY, ((memory.key, entity.key, {}),), scope=alice
    )
    links = client.get(BASE + "/nodes/" + memory.key + "/cross-graph").json()["data"]
    assert {n["other_key"] for n in links} == {metric.key, entity.key}
    assert {n["other_zone"] for n in links} == {"metadata", "knowledge"}
    assert {n["direction"] for n in links} == {"out"}
    counts = client.get(BASE + "/schema").json()["data"]["relationship_types"]
    assert [r["count"] for r in counts if r["type"] == "REFERS_TO"] == [2]
    detail = client.post(
        BASE + "/edge-detail",
        json={
            "source_key": memory.key,
            "target_key": metric.key,
            "rel_type": "REFERS_TO",
        },
    ).json()["data"]
    assert detail["is_cross_graph"] is True
    identity["value"] = authorization(BOB)
    assert client.get(BASE + "/nodes/" + memory.key + "/cross-graph").status_code == 404


def test_exploration_http_stream_uses_the_callers_personal_graph(
    app_graph, monkeypatch
):
    client, _, runtime, identity, *_ = app_graph
    identity["value"] = authorization(ADMIN, role="ws_admin")
    monkeypatch.setattr(runtime, "models", lambda context: object())
    captured = []

    def prepare(service, scope, manager, query, **options):
        captured.append((scope, query, options))

        async def events():
            yield 'event: final\ndata: {"text":"Done"}\n\n'
            yield "event: done\ndata: {}\n\n"

        return events()

    monkeypatch.setattr(
        "context_manager.api.legacy.explorer.graph_center.prepare_exploration", prepare
    )
    response = client.post(
        "/api/v3/cm/explore", json={"query": " revenue ", "max_rounds": 4, "lang": "en"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: final" in response.text and "event: done" in response.text
    scope, query, options = captured[0]
    assert scope.owner_ids == (ADMIN,)
    assert query == "revenue" and options == {"max_rounds": 4, "lang": "en"}
