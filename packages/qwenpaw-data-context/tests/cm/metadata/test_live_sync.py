"""Verify Metadata sync atomically against a disposable local Neo4j instance."""

from dataclasses import replace

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.metadata import MetadataSyncService
from context_manager.schema import DOMAIN, HAS_METRIC, METRIC, MG_SYNC_STATE
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import Neo4jGraphStore
from context_manager.transaction import StoreOperation
from qwenpaw_data_auth.config import AuthConfig
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.schema_support import make_glossary
from cm.metadata.test_sync import create, seed_metadata


@pytest.fixture
def sync_graph(graph):  # noqa: F811 - imported pytest fixture
    store, runtime, (knowledge, _) = graph
    workspace = knowledge.workspace_id
    shared = store.access.issue_write_scope(
        authorization("admin", workspace_id=workspace, role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        zone="metadata",
    )
    alice = store.access.issue_write_scope(
        authorization("alice", workspace_id=workspace),
        owner=WriteOwner.USER,
    )
    bob = store.access.issue_write_scope(
        authorization("bob", workspace_id=workspace),
        owner=WriteOwner.USER,
    )
    return store, runtime, shared, alice, bob


def test_neo4j_failure_in_final_batch_rolls_back_earlier_datasources_and_state(
    sync_graph,
    database_engine,
):
    store, _, shared, alice, _ = sync_graph
    seed_metadata(store, shared, "east")
    seed_metadata(store, shared, "west")
    service = MetadataSyncService(store, database_engine=database_engine)
    plan = service._prepare_plan(alice)
    # A valid operation shape with an invalid required property fails in the final Store batch.
    invalid = StoreOperation(
        "create_nodes",
        {
            "spec": DOMAIN,
            "properties": ({"domain_id": "invalid"},),
        },
    )
    plan = replace(plan, operations=(*plan.operations, (None, invalid)))
    with pytest.raises(ValueError, match="required"):
        service._execute_plan(plan, alice)
    assert store.list_nodes(METRIC, scope=alice) == ()
    assert store.list_nodes(MG_SYNC_STATE, scope=alice) == ()
    assert store.list_edges(HAS_METRIC, scope=alice) == ()
    assert len(store.list_nodes(METRIC, scope=shared)) == 2


def test_http_pull_and_conflict_decisions_round_trip_through_neo4j(
    sync_graph, monkeypatch, tmp_path
):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    store, graph_runtime, shared, alice, bob = sync_graph
    source_scope = store.access.narrow_write_scope(shared, datasource_id="warehouse")
    source = create(store, source_scope, DOMAIN, domain_id="sales", name="Sales")
    metric = create(store, source_scope, METRIC, domain="sales", name="revenue")
    store.create_edges(HAS_METRIC, ((source.key, metric.key, {}),), scope=source_scope)
    api_store = Neo4jGraphStore(
        graph_runtime,
        store.schema,
        store.access,
        make_glossary(store.schema),
        owns_runtime=False,
    )
    runtime = bound_context_runtime(
        api_store,
        RdbConnector(),
        make_glossary(store.schema),
        workspace_id=shared.workspace_id,
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)
    monkeypatch.setattr(
        "context_manager.api.native.metadata.userspace.log_event",
        lambda *args, **kwargs: None,
    )
    install_context_authentication(
        app,
        AuthConfig(secret_keys=["test"], control_plane_dsn="sqlite:///:memory:"),
        runtime.fixed_target,
        authorizer=lambda request: authorization(
            request.headers["x-test-user"], workspace_id=shared.workspace_id
        ),
    )
    try:
        # The app borrows the fixture's Store runtime; closing it leaves the driver available for cleanup.
        with TestClient(app) as client:
            alice_headers = {"x-test-user": "alice"}
            bob_headers = {"x-test-user": "bob"}
            appended = client.post(
                "/api/v1/mg/userspace/nodes",
                headers=alice_headers,
                json={
                    "label": "Domain",
                    "props": {"domain_id": "sales", "name": "My sales"},
                },
            )
            assert appended.status_code == 200, appended.text
            first = client.post("/api/v1/mg/userspace/sync", headers=alice_headers)
            assert first.status_code == 200, first.text
            conflict = first.json()["data"]
            assert conflict["conflict_count"] == 1
            keep = client.post(
                "/api/v1/mg/userspace/sync/resolve",
                headers=alice_headers,
                json={
                    "global_digest": conflict["global_digest"],
                    "resolutions": [{"key": source.key, "decision": "user"}],
                },
            )
            assert keep.json()["data"] == {"resolved": 1, "synced": True}
            assert (
                client.get(
                    "/api/v1/mg/userspace/sync-status", headers=alice_headers
                ).json()["data"]["needs_sync"]
                is False
            )
            assert (
                store.list_nodes(DOMAIN, scope=alice)[0].properties["name"]
                == "My sales"
            )
            assert store.list_edges(HAS_METRIC, scope=alice) == ()
            replay = client.post("/api/v1/mg/userspace/sync", headers=alice_headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["data"]["created"] == 1
            assert (
                store.list_edges(HAS_METRIC, scope=alice)[0].properties.get(
                    "datasource_id"
                )
                is None
            )
            assert store.list_nodes(DOMAIN, scope=bob) == ()
            store.patch_nodes(
                DOMAIN, ((source.key, {"name": "Commerce"}),), scope=shared
            )
            stale = client.post(
                "/api/v1/mg/userspace/sync/resolve",
                headers=alice_headers,
                json={
                    "global_digest": conflict["global_digest"],
                    "resolutions": [{"key": source.key, "decision": "global"}],
                },
            )
            assert stale.status_code == 409
            second = client.post(
                "/api/v1/mg/userspace/sync", headers=alice_headers
            ).json()["data"]
            resolved = client.post(
                "/api/v1/mg/userspace/sync/resolve",
                headers=alice_headers,
                json={
                    "global_digest": second["global_digest"],
                    "resolutions": [{"key": source.key, "decision": "global"}],
                },
            )
            assert resolved.json()["data"] == {"resolved": 1, "synced": True}
            assert (
                store.list_nodes(DOMAIN, scope=alice)[0].properties["name"]
                == "Commerce"
            )
            assert (
                store.list_nodes(DOMAIN, scope=alice)[0].properties["datasource_id"]
                == "warehouse"
            )
            assert (
                store.list_edges(HAS_METRIC, scope=alice)[0].properties["datasource_id"]
                == "warehouse"
            )
            assert (
                client.post(
                    "/api/v1/mg/userspace/sync", headers=bob_headers
                ).status_code
                == 200
            )
            assert (
                store.list_nodes(DOMAIN, scope=bob)[0].properties["name"] == "Commerce"
            )
            assert (
                store.get_nodes(DOMAIN, (source.key,), scope=shared)[0].properties[
                    "name"
                ]
                == "Commerce"
            )
    finally:
        runtime.close()
