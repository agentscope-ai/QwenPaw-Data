"""Run batch contracts against a disposable local CM_BATCH_NEO4J_URI database."""

from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from context_manager.auth import GraphScopeAuthority, WriteOwner
from context_manager.schema import ENTITY, GRAPH_SCHEMA, HIERARCHY
from context_manager.store import (
    StoreError,
)
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from context_manager.store.neo4j.catalog import Neo4jSchemaCatalog
from context_manager.store.neo4j.physical_schema import (
    NEO4J_PHYSICAL_SCHEMA,
    ensure_schema,
)
from neo4j import GraphDatabase

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.schema_support import make_glossary


@pytest.fixture(scope="module")
def local_driver():
    uri = os.environ.get("CM_BATCH_NEO4J_URI")
    if not uri:
        pytest.skip("set CM_BATCH_NEO4J_URI to a disposable local Neo4j database")
    if urlparse(uri).hostname not in {"127.0.0.1", "localhost"}:
        pytest.fail("batch integration tests require a disposable local database")
    password = os.environ.get("CM_BATCH_NEO4J_PASSWORD")
    with GraphDatabase.driver(
        uri, auth=("neo4j", password) if password else None
    ) as driver:
        driver.verify_connectivity()
        yield driver


@pytest.fixture
def graph(local_driver):
    workspace_id = f"cm-batch-test-{uuid4().hex}"
    runtime = Neo4jRuntime(local_driver, GraphTarget("tenant-1", workspace_id))
    access = GraphScopeAuthority()
    ensure_schema(
        runtime, Neo4jSchemaCatalog(GRAPH_SCHEMA, NEO4J_PHYSICAL_SCHEMA.constraints)
    )
    store = Neo4jGraphStore(
        runtime,
        GRAPH_SCHEMA,
        access,
        make_glossary(GRAPH_SCHEMA),
        catalog=Neo4jSchemaCatalog(GRAPH_SCHEMA, NEO4J_PHYSICAL_SCHEMA.constraints),
    )
    scopes = tuple(
        access.issue_write_scope(
            authorization(user_id=user, workspace_id=workspace_id),
            owner=WriteOwner.USER,
            zone="knowledge",
        )
        for user in ("alice", "bob")
    )
    yield store, runtime, scopes
    runtime.write(
        "MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n",
        {"workspace_id": workspace_id},
        operation="cleanup_batch_fixture",
    )


def entities(store, scope, count=3):
    keys = store.create_nodes(
        ENTITY,
        tuple(
            {"identity_hash": str(i), "type": "product", "name": f"产品 {i}"}
            for i in range(count)
        ),
        scope=scope,
    )

    return store.get_nodes(ENTITY, keys, scope=scope)


def test_batch_crud_round_trip_preserves_order_missing_entries_and_other_fields(graph):
    store, _, (scope, _) = graph
    a, b, c = entities(store, scope)
    assert store.get_nodes(ENTITY, (b.key, "missing", a.key, b.key), scope=scope) == (
        b,
        None,
        a,
        b,
    )

    assert (
        store.patch_nodes(
            ENTITY,
            (
                (b.key, {"description": "新增说明"}),
                ("missing", {"name": "缺失"}),
                (a.key, {"name": "新名称"}),
            ),
            scope=scope,
        )
        is None
    )
    updated_a, updated_b = store.get_nodes(ENTITY, (a.key, b.key), scope=scope)
    assert updated_a.properties["name"] == "新名称"
    assert updated_a.properties["type"] == "product"
    assert updated_b.properties["name"] == b.properties["name"]
    assert updated_b.properties["description"] == "新增说明"

    edges = store.create_edges(
        HIERARCHY,
        (
            (b.key, c.key, {"description": "下游"}),
            (a.key, b.key, {"description": "上游"}),
        ),
        scope=scope,
    )
    assert tuple((e.source_key, e.target_key) for e in edges) == (
        (b.key, c.key),
        (a.key, b.key),
    )
    assert store.get_edges(
        HIERARCHY, ((a.key, b.key), (c.key, a.key), (a.key, b.key)), scope=scope
    ) == (
        edges[1],
        None,
        edges[1],
    )
    assert (
        store.patch_edges(
            HIERARCHY,
            (
                (b.key, c.key, {"description": "更新"}),
                (c.key, a.key, {"description": "缺失"}),
            ),
            scope=scope,
        )
        is None
    )
    (updated_edge,) = store.get_edges(HIERARCHY, ((b.key, c.key),), scope=scope)
    assert updated_edge.properties["description"] == "更新"
    assert updated_edge.properties["owner_id"] == "alice"
    assert (
        store.delete_edges(
            HIERARCHY,
            ((b.key, c.key), (c.key, a.key), (a.key, b.key)),
            scope=scope,
        )
        is None
    )
    assert (
        store.delete_nodes(ENTITY, (c.key, "missing", a.key, b.key), scope=scope)
        is None
    )
    assert store.get_nodes(ENTITY, (a.key, b.key, c.key), scope=scope) == (
        None,
        None,
        None,
    )


def test_batch_operations_cannot_read_patch_delete_or_link_another_users_graph(graph):
    store, _, (alice, bob) = graph
    a, b, _ = entities(store, alice)
    (own,) = entities(store, bob, count=1)
    store.create_edges(
        HIERARCHY, ((a.key, b.key, {"description": "私有关系"}),), scope=alice
    )
    assert store.get_nodes(ENTITY, (a.key, own.key), scope=bob) == (None, own)
    assert store.get_edges(HIERARCHY, ((a.key, b.key),), scope=bob) == (None,)
    assert (
        store.patch_nodes(ENTITY, ((a.key, {"name": "不可修改"}),), scope=bob) is None
    )
    assert store.delete_nodes(ENTITY, (a.key,), scope=bob) is None
    assert (
        store.patch_edges(
            HIERARCHY, ((a.key, b.key, {"description": "不可修改"}),), scope=bob
        )
        is None
    )
    assert store.delete_edges(HIERARCHY, ((a.key, b.key),), scope=bob) is None
    with pytest.raises(StoreError, match="every input"):
        store.create_edges(HIERARCHY, ((own.key, a.key, {}),), scope=bob)
    assert store.get_nodes(ENTITY, (a.key, b.key), scope=alice) == (a, b)
    assert (
        store.get_edges(HIERARCHY, ((a.key, b.key),), scope=alice)[0].properties[
            "description"
        ]
        == "私有关系"
    )


def test_missing_endpoint_rolls_back_preceding_node_batches_in_atomic(graph):
    store, _, (scope, _) = graph

    def work(tx):
        a, b, _ = entities(tx, scope)
        tx.create_edges(
            HIERARCHY, ((a.key, b.key, {}), (b.key, "missing", {})), scope=scope
        )

    with pytest.raises(StoreError, match="every input"):
        store.atomic(work)
    assert store.list_nodes(ENTITY, scope=scope) == ()
    assert store.list_edges(HIERARCHY, scope=scope) == ()


def test_database_constraint_failure_rolls_back_the_entire_node_batch(graph):
    store, _, (scope, _) = graph
    (existing,) = entities(store, scope, count=1)
    with pytest.raises(StoreError):
        store.create_nodes(
            ENTITY,
            (
                {"identity_hash": "new", "type": "product"},
                {"identity_hash": "0", "type": "product"},
            ),
            scope=scope,
        )
    assert store.list_nodes(ENTITY, scope=scope) == (existing,)


@pytest.mark.parametrize(
    "operation",
    [
        "create_nodes",
        "patch_nodes",
        "delete_nodes",
        "create_edges",
        "patch_edges",
        "delete_edges",
    ],
)
def test_caught_write_failure_cannot_commit_preceding_changes(graph, operation):
    store, _, (scope, _) = graph

    def work(tx):
        a, b, _ = entities(tx, scope)
        try:
            if operation == "create_nodes":
                tx.create_nodes(ENTITY, ({"identity_hash": "invalid"},), scope=scope)
            elif operation == "patch_nodes":
                tx.patch_nodes(ENTITY, ((a.key, {"unknown": True}),), scope=scope)
            elif operation == "delete_nodes":
                tx.delete_nodes(ENTITY, (a.key, a.key), scope=scope)
            elif operation == "create_edges":
                tx.create_edges(
                    HIERARCHY, ((a.key, b.key, {}), (b.key, "missing", {})), scope=scope
                )
            elif operation == "patch_edges":
                tx.patch_edges(HIERARCHY, ((a.key, b.key, {}),), scope=scope)
            else:
                tx.delete_edges(
                    HIERARCHY, ((a.key, b.key), (a.key, b.key)), scope=scope
                )
        except (ValueError, TypeError, StoreError):
            pass

    with pytest.raises(StoreError) as failure:
        store.atomic(work)
    assert failure.value.code == "transaction_failed"
    assert store.list_nodes(ENTITY, scope=scope) == ()
    assert store.list_edges(HIERARCHY, scope=scope) == ()


def test_search_projection_preserves_index_ranking_and_scoped_results(graph):
    original, runtime, (alice, bob) = graph
    store = Neo4jGraphStore(
        runtime,
        GRAPH_SCHEMA,
        original.access,
        make_glossary(GRAPH_SCHEMA),
        catalog=Neo4jSchemaCatalog(
            GRAPH_SCHEMA,
            NEO4J_PHYSICAL_SCHEMA.constraints,
            tuple(
                index for index in NEO4J_PHYSICAL_SCHEMA.indexes if index.node is ENTITY
            ),
        ),
    )
    vector = (0.25,) * 1024
    for scope in (alice, bob):
        store.create_nodes(
            ENTITY,
            (
                {
                    "identity_hash": "projected-search",
                    "type": "product",
                    "name": "projectionfixture",
                    "embedding": vector,
                },
            ),
            scope=scope,
        )
    for search, query in (
        (store.search_fulltext, "projectionfixture"),
        (store.search_vector, vector),
    ):
        full = search(query, specs=(ENTITY,), scope=alice)
        projected = search(query, specs=(ENTITY,), fields=("name",), scope=alice)
        assert len(full) == len(projected) == 1
        assert [row.key for row in projected] == [row.key for row in full]
        assert projected[0].properties == {
            "key": full[0].key,
            "name": "projectionfixture",
        }
        assert full[0].properties["owner_id"] == "alice"
        assert full[0].properties["embedding"] == vector

    store.patch_nodes(
        ENTITY, ((full[0].key, {"embedding": [-v for v in vector]}),), scope=alice
    )
    store.create_nodes(
        ENTITY,
        tuple(
            {"identity_hash": f"competitor-{i}", "type": "product", "embedding": vector}
            for i in range(20)
        ),
        scope=bob,
    )
    assert [
        n.key
        for n in store.search_vector(vector, specs=(ENTITY,), limit=1, scope=alice)
    ] == [full[0].key]
