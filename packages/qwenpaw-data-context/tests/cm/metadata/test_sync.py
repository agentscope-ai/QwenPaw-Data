"""Exercise userspace pulls through the Metadata business service."""

import pytest
from context_manager.auth import WriteOwner
from context_manager.metadata import MetadataSyncConflict, MetadataSyncService
from context_manager.schema import (
    COLUMN,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    HAS_METRIC,
    METRIC,
    METRIC_CALIBER,
    TABLE,
    VERIFIED_QUERY,
)
from context_manager.sql import DatasourceRepository

from cm.infrastructure.access.support import authorization
from cm.knowledge.support import MemoryGraphStore
from cm.retrieval.sql.support import create_catalog


@pytest.fixture
def sync_graph():
    store = MemoryGraphStore()
    admin = authorization("admin", role="ws_admin")
    shared = store.access.issue_write_scope(
        admin, owner=WriteOwner.WORKSPACE, zone="metadata"
    )
    alice = store.access.issue_write_scope(
        authorization("alice"), owner=WriteOwner.USER
    )
    bob = store.access.issue_write_scope(authorization("bob"), owner=WriteOwner.USER)
    return store, shared, alice, bob


def create(store, scope, spec, **properties):
    return store.get_nodes(
        spec, store.create_nodes(spec, (properties,), scope=scope), scope=scope
    )[0]


def seed_metadata(store, shared, datasource="warehouse", *, embedding_dim=2):
    scope = store.access.narrow_write_scope(shared, datasource_id=datasource)
    nodes = [
        create(
            store,
            scope,
            DATA_SOURCE,
            datasource_name="Warehouse",
            datasource_type="mysql",
        ),
        create(store, scope, DOMAIN, domain_id=f"sales-{datasource}", name="Sales"),
        create(
            store,
            scope,
            DATASET,
            domain=f"sales-{datasource}",
            name="orders",
            source="SELECT * FROM orders",
        ),
        create(
            store,
            scope,
            DATASET_FIELD,
            domain=f"sales-{datasource}",
            dataset="orders",
            name="amount",
        ),
        create(store, scope, DIMENSION, domain=f"sales-{datasource}", name="region"),
        create(
            store,
            scope,
            METRIC,
            domain=f"sales-{datasource}",
            name="revenue",
            description="Sales revenue",
            embedding=[1.0] + [0.0] * (embedding_dim - 1),
            embedding_hash="old-vector",
        ),
        create(
            store,
            scope,
            METRIC_CALIBER,
            domain=f"sales-{datasource}",
            metric_name="revenue",
            dataset="orders",
            time_window="",
            expr="sum(amount)",
        ),
        create(
            store,
            scope,
            VERIFIED_QUERY,
            domain=f"sales-{datasource}",
            question="What is revenue?",
            question_hash="q1",
            verified_sql="SELECT SUM(amount) FROM orders",
        ),
        create(store, scope, TABLE, name="orders"),
        create(
            store, scope, COLUMN, table="orders", name="amount", data_type="decimal"
        ),
    ]
    store.create_edges(HAS_METRIC, ((nodes[1].key, nodes[5].key, {}),), scope=scope)
    return nodes


def test_full_pull_mirrors_all_metadata_types_without_vectors_or_credentials(
    sync_graph, tmp_path, database_engine
):
    store, shared, alice, bob = sync_graph
    source = seed_metadata(store, shared)
    service = MetadataSyncService(store, database_engine=database_engine)
    result = service.sync(alice)
    assert result["conflicts"] == []
    assert service.status(alice)["needs_sync"] is False
    assert service.status(bob)["needs_sync"] is True
    for original in source:
        private = store.list_nodes(original.spec, scope=alice)
        assert len(private) == 1
        assert private[0].properties["owner_id"] == "alice"
        assert private[0].key != original.key
        assert private[0].properties.get("config") is None
        assert private[0].properties.get("embedding") is None
        assert private[0].properties.get("embedding_hash") is None
        assert not store.list_nodes(original.spec, scope=bob)
        assert store.get_nodes(original.spec, (original.key,), scope=shared) == (
            original,
        )
    private_source = store.list_nodes(DATA_SOURCE, scope=alice)[0]
    _dsn = create_catalog(
        tmp_path, datasource_type="mysql", config={"password": "catalog-secret"}
    )
    datasource = DatasourceRepository().get(
        private_source.properties["datasource_id"],
        tenant_id=alice.tenant_id,
        workspace_id=alice.workspace_id,
    )
    assert datasource.config["password"] == "catalog-secret"
    assert (service.sync(alice)["overwritten"], service.sync(alice)["created"]) == (
        0,
        0,
    )
    service.sync(bob)
    assert (
        store.list_nodes(METRIC, scope=bob)[0].key
        != store.list_nodes(METRIC, scope=alice)[0].key
    )


def test_mirror_updates_replace_removed_facts_and_leave_personal_additions(
    sync_graph, database_engine
):
    store, shared, alice, _ = sync_graph
    source = create(
        store, shared, DOMAIN, domain_id="sales", name="Sales", description="Old"
    )
    service = MetadataSyncService(store, database_engine=database_engine)
    service.sync(alice)
    local = create(store, alice, DOMAIN, domain_id="personal", name="My analysis")
    store.patch_nodes(
        DOMAIN, ((source.key, {"name": "Commerce", "description": None}),), scope=shared
    )
    assert service.status(alice)["needs_sync"] is True
    assert service.sync(alice)["conflicts"] == []
    copies = {
        node.properties["domain_id"]: node
        for node in store.list_nodes(DOMAIN, scope=alice)
    }
    assert copies["sales"].properties["name"] == "Commerce"
    assert copies["sales"].properties.get("description") is None
    assert copies["personal"] == local


def test_conflict_defers_edges_and_supports_keep_user_then_keep_global(
    sync_graph, database_engine
):
    store, shared, alice, _ = sync_graph
    domain = create(store, shared, DOMAIN, domain_id="sales", name="Sales")
    metric = create(
        store, shared, METRIC, domain="sales", name="revenue", description="Gross"
    )
    store.create_edges(HAS_METRIC, ((domain.key, metric.key, {}),), scope=shared)
    own = create(
        store, alice, METRIC, domain="sales", name="revenue", description="Net"
    )
    service = MetadataSyncService(store, database_engine=database_engine)
    first = service.sync(alice)
    assert first["conflict_count"] == 1
    assert first["conflicts"][0]["key"] == metric.key
    assert store.list_edges(HAS_METRIC, scope=alice) == ()
    assert service.status(alice)["needs_sync"] is True
    assert service.resolve(
        first["global_digest"], [{"key": metric.key, "decision": "user"}], alice
    ) == {"resolved": 1, "synced": True}
    assert store.get_nodes(METRIC, (own.key,), scope=alice) == (own,)
    assert store.list_edges(HAS_METRIC, scope=alice) == ()
    assert service.sync(alice)["conflicts"] == []
    store.patch_nodes(
        METRIC, ((metric.key, {"description": "Updated gross"}),), scope=shared
    )
    with pytest.raises(MetadataSyncConflict):
        service.resolve(
            first["global_digest"], [{"key": metric.key, "decision": "global"}], alice
        )
    second = service.sync(alice)
    assert second["conflict_count"] == 1
    assert (
        service.resolve(
            second["global_digest"], [{"key": metric.key, "decision": "global"}], alice
        )["synced"]
        is True
    )
    assert (
        store.get_nodes(METRIC, (own.key,), scope=alice)[0].properties["description"]
        == "Updated gross"
    )
    assert len(store.list_edges(HAS_METRIC, scope=alice)) == 1


@pytest.mark.parametrize(
    "restored_name,conflicts", [("Sales", 0), ("New authority", 1)]
)
def test_keep_user_decision_survives_source_absence_and_only_conflicts_with_changed_content(
    sync_graph, restored_name, conflicts, database_engine
):
    store, shared, alice, _ = sync_graph
    source = create(store, shared, DOMAIN, domain_id="sales", name="Sales")
    own = create(store, alice, DOMAIN, domain_id="sales", name="My sales")
    service = MetadataSyncService(store, database_engine=database_engine)
    result = service.sync(alice)
    service.resolve(
        result["global_digest"], [{"key": source.key, "decision": "user"}], alice
    )
    store.delete_nodes(DOMAIN, (source.key,), scope=shared)

    assert service.sync(alice)["deleted"] == 0
    assert store.get_nodes(DOMAIN, (own.key,), scope=alice) == (own,)
    create(store, shared, DOMAIN, domain_id="sales", name=restored_name)
    assert service.sync(alice)["conflict_count"] == conflicts
    assert store.get_nodes(DOMAIN, (own.key,), scope=alice) == (own,)
