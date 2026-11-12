"""Exercise CM v19 schema ownership and SQL-backed glossary contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, inspect

from context_manager.schema import (
    DEFAULT_GLOSSARY_FILE,
    ENTITY,
    GRAPH_SCHEMA,
    CrudOperation,
    DisplayPolicy,
    Glossary,
    GlossaryRepository,
    GraphSchema,
    KeySpec,
    KeyStrategy,
    NodeSpec,
    PropertyOwner,
    PropertySpec,
    UnknownTermError,
    ValueKind,
    Zone,
)
from context_manager.schema import relational


def test_v19_topology_and_recovery_nodes_are_preserved():
    assert GRAPH_SCHEMA.version == 19
    assert len(GRAPH_SCHEMA.nodes) == 17
    assert len(GRAPH_SCHEMA.edges) == 30
    assert set(GRAPH_SCHEMA.zones) == set(Zone)
    internal = {node.name for node in GRAPH_SCHEMA.nodes if node.internal}
    assert internal == {"MGSyncState", "RecoveryStackHead", "UndoBatch"}
    head = GRAPH_SCHEMA.node("RecoveryStackHead")
    assert {"rid", "recovery_state", "head_seq", "result_status", "message"} <= {
        prop.name for prop in head.properties
    }
    assert {"stack_key", "sequence", "payload"} <= {
        prop.name for prop in GRAPH_SCHEMA.node("UndoBatch").properties
    }
    overview = GRAPH_SCHEMA.get_overview()
    assert set(overview["zones"]) == {"metadata", "knowledge", "user"}
    assert not internal & {node["name"] for node in overview["nodes"]}
    for public in overview["nodes"]:
        node = GRAPH_SCHEMA.node(public["name"])
        assert all(node.property(name).display_policy is not DisplayPolicy.NEVER
                   for name in public["properties"])
    assert all(edge.source is GRAPH_SCHEMA.node(edge.source.name)
               and edge.target is GRAPH_SCHEMA.node(edge.target.name)
               for edge in GRAPH_SCHEMA.edges)


def test_canonical_identity_cannot_be_replaced_by_equal_declarations():
    clone = replace(ENTITY)
    assert clone == ENTITY and clone is not ENTITY
    with pytest.raises(ValueError, match="canonical"):
        GRAPH_SCHEMA.require_node(clone)
    edge = GRAPH_SCHEMA.edges[0]
    with pytest.raises(ValueError, match="canonical"):
        GRAPH_SCHEMA.require_edge(replace(edge))
    with pytest.raises(ValueError, match="non-canonical"):
        GraphSchema(19, GRAPH_SCHEMA.nodes, (replace(edge, source=replace(edge.source)),))
    with pytest.raises(TypeError):
        GRAPH_SCHEMA.nodes_by_name["Entity"] = clone


def test_deployment_document_and_fingerprint_are_complete_and_detached():
    document = GRAPH_SCHEMA.to_document()
    assert "_internal" in document["zones"]
    assert any(node["name"] == "UndoBatch" for node in document["nodes"])
    payload = json.dumps(document, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode()
    assert GRAPH_SCHEMA.fingerprint == hashlib.sha256(payload).hexdigest()
    document["nodes"].clear()
    assert len(GRAPH_SCHEMA.to_document()["nodes"]) == 17
    assert replace(GRAPH_SCHEMA, version=20).fingerprint != GRAPH_SCHEMA.fingerprint


@pytest.mark.parametrize("node_name,name", [
    ("Entity", "key"), ("Entity", "workspace_id"), ("Entity", "owner_id"),
    ("Entity", "zone"), ("Dataset", "datasource_id"),
])
def test_managed_properties_cannot_be_forged_or_patched(node_name, name):
    node = GRAPH_SCHEMA.node(node_name)
    assert node.property(name).owner is not PropertyOwner.CALLER
    for operation in (CrudOperation.CREATE, CrudOperation.UPDATE):
        with pytest.raises(PermissionError):
            GRAPH_SCHEMA.validate_operation(node, operation, (name,))


def test_key_contract_and_immutable_deterministic_parts():
    generated = KeySpec(strategy=KeyStrategy.GENERATED, prefix="item")
    assert generated.generate({}).startswith("item_")
    assert generated.generate({}) != generated.generate({})
    deterministic = KeySpec(strategy=KeyStrategy.DETERMINISTIC, prefix="item", parts=("id",))
    assert deterministic.generate({"id": "42"}) == "item:42"
    with pytest.raises(ValueError, match="missing"):
        deterministic.generate({})
    with pytest.raises(ValueError, match="editable"):
        NodeSpec("Item", Zone.METADATA, frozenset(CrudOperation),
                 (deterministic, PropertySpec("id", ValueKind.TEXT, nullable=False)))
    with pytest.raises(ValueError, match="nullable"):
        NodeSpec("Item", Zone.METADATA, frozenset(CrudOperation),
                 (deterministic, PropertySpec("id", ValueKind.TEXT, editable=False)))
    with pytest.raises(ValueError, match="exactly one"):
        NodeSpec("Item", Zone.METADATA, frozenset(CrudOperation),
                 (PropertySpec("name", ValueKind.TEXT),))


@pytest.mark.parametrize("kind,value", [
    (ValueKind.BOOLEAN, 1), (ValueKind.INTEGER, True),
    (ValueKind.FLOAT, float("nan")), (ValueKind.FLOAT, float("inf")),
    (ValueKind.TEXT_LIST, "text"), (ValueKind.TEXT_LIST, ["text", 1]),
    (ValueKind.VECTOR, [True, 1.0]), (ValueKind.VECTOR, [float("inf")]),
])
def test_logical_kinds_reject_malformed_values(kind, value):
    node = NodeSpec("Item", Zone.USER, frozenset(CrudOperation),
                    (KeySpec(strategy=KeyStrategy.GENERATED, prefix="item"),
                     PropertySpec("value", kind, nullable=False)))
    schema = GraphSchema(1, (node,), ())
    with pytest.raises(TypeError):
        schema.validate_values(node, {"value": value})
    with pytest.raises(ValueError, match="cannot be null"):
        schema.validate_values(node, {"value": None})
    with pytest.raises(ValueError, match="missing"):
        schema.validate_values(node, {}, complete=True)


def test_cm_ddl_never_creates_external_auth_usage_or_history_projections():
    engine = create_engine("sqlite://")
    relational.metadata.create_all(engine)
    owned = set(inspect(engine).get_table_names())
    assert owned == set(relational.metadata.tables)
    assert {"cm_recovery_runs", "cm_embedding_jobs", "cm_cron_jobs",
            "cm_user_model_config", "cm_kg_glossary"} <= owned
    external = (*relational.external_tables, relational.model_usages)
    assert {table.name for table in external}.isdisjoint(owned)
    assert {"sessions", "chats", "chat_events", "model_usages",
            "users", "tenants", "workspaces", "role_bindings"} <= {
        table.name for table in external
    }
    assert all(len(table.c) > 0 for table in external)
    assert set(relational.role_bindings.c.keys()) == {"user_id", "scope_type", "scope_id", "role"}
    assert not {"buc_id", "sso_id", "authx_id"} & set(relational.users.c.keys())


@pytest.fixture
def glossary_engine():
    engine = create_engine("sqlite://")
    relational.workspace_glossaries.create(engine)
    yield engine
    engine.dispose()


def _glossary(engine, tenant="tenant-1", workspace="workspace-1"):
    return Glossary(GRAPH_SCHEMA, engine, tenant_id=tenant, workspace_id=workspace)


def test_glossary_mutations_persist_without_modifying_or_reseeding_resource(glossary_engine):
    # Ported from CM metadata/test_glossary.py; initialize only the owned table.
    before = DEFAULT_GLOSSARY_FILE.read_bytes()
    first = _glossary(glossary_engine)
    first.upsert_term(ENTITY, "type", "regulation", description="法规")
    first.set_graph_labels(ENTITY, "type", "regulation", frozenset({"Core"}))
    updated = first.upsert_term(ENTITY, "type", "regulation", description="法律法规")
    peer = _glossary(glossary_engine)
    assert peer.resolve_term(ENTITY, "type", "regulation") == updated
    assert updated.graph_labels == frozenset({"Core"})
    for term in peer.list_terms(ENTITY, "type"):
        assert peer.remove_term(ENTITY, "type", term.value)
    assert not first.remove_term(ENTITY, "type", "regulation")
    assert _glossary(glossary_engine).list_terms(ENTITY, "type") == ()
    assert DEFAULT_GLOSSARY_FILE.read_bytes() == before


@pytest.mark.parametrize("tenant,workspace", [("tenant-1", "workspace-2"), ("tenant-2", "workspace-1")])
def test_glossary_terms_are_shared_only_inside_exact_workspace(glossary_engine, tenant, workspace):
    first = _glossary(glossary_engine)
    first.upsert_term(ENTITY, "type", "regulation", description="法规")
    assert _glossary(glossary_engine).resolve_term(ENTITY, "type", "regulation").description == "法规"
    with pytest.raises(UnknownTermError):
        _glossary(glossary_engine, tenant, workspace).resolve_term(ENTITY, "type", "regulation")


def test_glossary_repository_edits_rollback_and_seed_preserves_core_catalog(glossary_engine):
    glossary = _glossary(glossary_engine)
    repository = GlossaryRepository(glossary_engine, tenant_id="tenant-1", workspace_id="workspace-1")
    before = repository.get()
    with pytest.raises(RuntimeError):
        with repository.edit() as document:
            document["term_groups"].clear()
            raise RuntimeError("abort")
    assert repository.get() == before
    terms = glossary.list_terms(ENTITY, "type")
    assert len(terms) == 12
    assert {term.value for term in terms if "Core" in term.graph_labels} == {
        "product", "platform", "model_family", "model_version", "subscription_plan"
    }
    assert "Qwen、Cursor、Qoder、Studio" in glossary.resolve_term(ENTITY, "type", "product").description
    with pytest.raises(ValueError, match="text property"):
        glossary.upsert_term(ENTITY, "embedding", "vector", description="vector")
    with pytest.raises(ValueError, match="only valid"):
        glossary.upsert_term(ENTITY, "name", "sample", description="sample")
        glossary.set_graph_labels(ENTITY, "name", "sample", frozenset({"Core"}))
    with pytest.raises(ValueError, match="unsupported"):
        glossary.set_graph_labels(ENTITY, "type", "product", frozenset({"Private"}))
