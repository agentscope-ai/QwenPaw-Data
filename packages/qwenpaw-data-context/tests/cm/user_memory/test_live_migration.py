"""Migrate synthetic legacy UG records in an opt-in disposable Neo4j database."""

from types import SimpleNamespace
from uuid import uuid4

from context_manager.api.app import create_api_app
from context_manager.api.dependencies import (
    require_context_authorization,
    require_context_runtime,
)
from context_manager.auth import GraphScopeAuthority
from context_manager.migration import migrate_user_graph
from context_manager.schema import GRAPH_SCHEMA
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from context_manager.store.neo4j.catalog import Neo4jSchemaCatalog
from context_manager.store.neo4j.physical_schema import NEO4J_PHYSICAL_SCHEMA
from context_manager.user.crud import MemoryCrudService
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.schema_support import make_glossary


def test_existing_ug_nodes_and_edges_are_readable_after_in_place_migration(
    local_driver,  # noqa: F811
):
    workspace = f"cm-migration-test-{uuid4().hex}"
    runtime = Neo4jRuntime(
        local_driver, GraphTarget("tenant-1", workspace, database="neo4j")
    )
    access = GraphScopeAuthority()
    store = Neo4jGraphStore(
        runtime,
        GRAPH_SCHEMA,
        access,
        make_glossary(GRAPH_SCHEMA),
        catalog=Neo4jSchemaCatalog(GRAPH_SCHEMA, NEO4J_PHYSICAL_SCHEMA.constraints),
    )
    preference_id, anchored_id = f"{workspace}-preference", f"{workspace}-rule"
    entity_key = f"ent:{workspace}:target"
    parameters = {
        "workspace_id": workspace,
        "preference_id": preference_id,
        "anchored_id": anchored_id,
        "entity_key": entity_key,
    }
    try:
        original = runtime.write(
            "CREATE (u:User {workspace_id: $workspace_id, owner_id: 'alice', user_id: 'alice', "
            "zone: 'user', status: 'active', profile: '{\"display_name\":\"Alice\"}'}) "
            "CREATE (p:UserMemory {workspace_id: $workspace_id, owner_id: 'alice', zone: 'user', "
            "id: $preference_id, type: 'preference', status: 'active', source_type: 'user_explicit', "
            "description: 'Use concise responses.'}) "
            "CREATE (m:UserMemory {workspace_id: $workspace_id, owner_id: 'alice', zone: 'user', "
            "key: 'mem:' + $anchored_id, "
            "id: $anchored_id, type: 'anchored_rule', status: 'active', source_type: 'user_explicit', "
            "description: 'Always check this concept.'}) "
            "CREATE (e:Entity {workspace_id: $workspace_id, owner_id: 'alice', zone: 'knowledge', "
            "key: $entity_key, name: 'Sales', type: 'concept'}) "
            "CREATE (u)-[a:HAS]->(p) CREATE (u)-[b:HAS]->(m) CREATE (m)-[c:REFERS_TO]->(e) "
            "RETURN [elementId(u), elementId(p), elementId(m), elementId(e)] AS nodes, "
            "[elementId(a), elementId(b), elementId(c)] AS edges",
            parameters,
            operation="seed_legacy_ug",
        )[0]
        app = create_api_app()
        app.dependency_overrides[require_context_authorization] = lambda: authorization(
            "alice", workspace_id=workspace
        )
        app.dependency_overrides[require_context_runtime] = lambda: SimpleNamespace(
            issue_graph_read_scope=access.issue_read_scope,
            user_crud=lambda scope: MemoryCrudService(store),
            run_graph_read=executor.run,
        )
        paths = (
            "/api/v1/ug/me",
            "/api/v1/ug/me/memories",
            "/api/v1/ug/me/global-memories",
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            assert [client.get(path).status_code for path in paths] == [500, 500, 500]
            preview = migrate_user_graph(runtime)
            assert preview["ready"] and not preview["applied"]
            assert preview["nodes"]["UserMemory"]["pending"] == 2
            result = migrate_user_graph(runtime, dry_run=False)
            assert result["nodes"]["User"]["updated"] == 1
            assert result["nodes"]["UserMemory"]["updated"] == 2
            assert result["edges"]["HAS"]["updated"] == 2
            assert result["edges"]["REFERS_TO"]["updated"] == 1
            responses = [client.get(path) for path in paths]
        assert [response.status_code for response in responses] == [200, 200, 200]
        assert responses[0].json()["data"]["profile"]["display_name"] == "Alice"
        memories = responses[1].json()["data"]
        assert responses[1].json()["meta"]["total"] == 2
        anchored = next(item for item in memories if item["id"] == anchored_id)
        assert anchored["references"][0]["key"] == entity_key
        assert responses[2].json()["preferences"][0]["id"] == preference_id
        persisted = runtime.read(
            "MATCH (n {workspace_id: $workspace_id}) RETURN elementId(n) AS element_id, "
            "properties(n) AS properties",
            {"workspace_id": workspace},
            operation="verify_legacy_ug_nodes",
        )
        assert {row["element_id"] for row in persisted} == set(original["nodes"])
        assert {row["properties"]["key"] for row in persisted} == {
            f"usr:{workspace}:alice",
            f"mem:alice:{preference_id}",
            f"mem:alice:{anchored_id}",
            entity_key,
        }
        edges = runtime.read(
            "MATCH (s {workspace_id: $workspace_id})-[r]->() RETURN elementId(r) AS element_id",
            {"workspace_id": workspace},
            operation="verify_legacy_ug_edges",
        )
        assert {row["element_id"] for row in edges} == set(original["edges"])
        replay = migrate_user_graph(runtime, dry_run=False)
        assert all(
            stats["pending"] == stats["updated"] == 0
            for group in ("nodes", "edges")
            for stats in replay[group].values()
        )
    finally:
        runtime.write(
            "MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n",
            {"workspace_id": workspace},
            operation="cleanup_migration_fixture",
        )
