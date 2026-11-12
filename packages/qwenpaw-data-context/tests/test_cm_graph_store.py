"""Test detached records, real catalogs/runtimes, and authenticated primitives.

Pure modules are loaded under an isolated name to avoid eager auth imports.
Their source and dependencies are unmodified; normal entrypoints are tested
separately, and missing dependencies remain explicit skips.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from context_manager import schema
from context_manager.schema import ENTITY, GRAPH_SCHEMA, Glossary
from context_manager.schema.relational import workspace_glossaries


def _load(leaf):
    name = f"context_manager._focused_store.{leaf}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(schema.__file__).resolve().parents[1] / "store" / (leaf.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        sys.modules.pop(name, None)
        pytest.skip(f"Real Store dependency unavailable: {exc}")
    return module


@pytest.fixture(scope="module")
def pure():
    return SimpleNamespace(
        models=_load("models"), runtime=_load("runtime"),
        neo_target=_load("neo4j.target"), neo_values=_load("neo4j.values"),
        neo_catalog=_load("neo4j.catalog"), neo_schema=_load("neo4j.physical_schema"),
        neo_runtime=_load("neo4j.runtime"),
        holo_catalog=_load("holoage.catalog"), holo_target=_load("holoage.target"),
        holo_schema=_load("holoage.physical_schema"),
    )


def test_drafts_are_immutable_request_local_values_not_persisted_keys(pure):
    values = {"name": "Qwen", "type": "product", "description": "model service"}
    draft = pure.models.NodeDraft.build(GRAPH_SCHEMA, ENTITY, "local-ref", values,
                                         transient={"lineage": ["document-1"]})
    values["name"] = "changed"
    assert draft.properties["name"] == "Qwen"
    assert draft.transient["lineage"] == ("document-1",)
    assert dict(draft.output()) == {"name": "Qwen", "type": "product", "description": "model service"}
    assert "ref" not in draft.output() and "lineage" not in draft.output()
    with pytest.raises(TypeError):
        draft.properties["name"] = "changed"
    with pytest.raises(PermissionError):
        pure.models.NodeDraft.build(GRAPH_SCHEMA, ENTITY, "local-ref", {"key": "forged"})
    edge = GRAPH_SCHEMA.edge(("Entity", "ASSOCIATION", "Entity"))
    candidate = pure.models.EdgeDraft.build(GRAPH_SCHEMA, edge, "left-ref", "right-ref",
                                            {"description": "linked"}, transient={"status": "new"})
    assert candidate.source_ref == "left-ref"
    assert dict(candidate.output()) == {"description": "linked"}
    with pytest.raises(PermissionError):
        pure.models.EdgeDraft.build(GRAPH_SCHEMA, edge, "left", "right", {"owner_id": "forged"})
    with pytest.raises(ValueError, match="canonical"):
        pure.models.NodeDraft.build(GRAPH_SCHEMA, replace(ENTITY), "ref", {})


def test_persisted_records_are_detached_and_subgraphs_are_endpoint_closed(pure):
    models = pure.models
    values = {"key": "e1", "name": "Qwen", "embedding": [1.0, 0.0]}
    first = models.NodeRecord(ENTITY, values)
    values["embedding"][0] = 0.0
    assert first.key == "e1" and first.properties["embedding"] == (1.0, 0.0)
    second = models.NodeRecord(ENTITY, {"key": "e2"})
    edge = models.EdgeRecord(GRAPH_SCHEMA.edge(("Entity", "ASSOCIATION", "Entity")), "e1", "e2", {})
    assert models.SubgraphRecord([first, second], [edge]).nodes == (first, second)
    with pytest.raises(ValueError, match="endpoints"):
        models.SubgraphRecord((first,), (edge,))
    with pytest.raises(ValueError, match="duplicate logical node"):
        models.SubgraphRecord((first, first), ())
    with pytest.raises(ValueError, match="duplicate logical edge"):
        models.SubgraphRecord((first, second), (edge, edge))
    with pytest.raises(ValueError):
        models.NodeRecord(ENTITY, {"key": " e1 "})
    with pytest.raises(TypeError, match="boolean"):
        models.SubgraphRecord((), (), truncated=1)


def test_native_graph_property_types_are_metadata_not_serialized_facts(pure):
    properties = pure.models.GraphPropertyMap({"key": "e1", "name": "Qwen"}, kind="node", types=("Entity",))
    assert properties.kind == "node" and properties.types == ("Entity",)
    assert json.loads(json.dumps(properties)) == {"key": "e1", "name": "Qwen"}
    from neo4j.graph import Graph, Node
    native = Node(Graph(), "n1", 1, frozenset({"Entity"}), {"key": "e1"})
    detached = pure.neo_values._detach_query_value(native)
    assert isinstance(detached, pure.models.GraphPropertyMap)
    assert detached.types == ("Entity",) and detached == {"key": "e1"}


def test_neo4j_catalog_derives_keys_fulltext_and_scope_indexes(pure):
    catalog = pure.neo_schema.NEO4J_PHYSICAL_SCHEMA
    assert catalog.schema is GRAPH_SCHEMA
    assert {item.node for item in catalog.constraints} == set(GRAPH_SCHEMA.nodes)
    assert all(item.properties == (item.node.property("key"),) for item in catalog.constraints)
    fulltext = {item.node: item for item in catalog.indexes if item.kind == "fulltext"}
    assert set(fulltext) == {node for node in GRAPH_SCHEMA.nodes if node.fulltext_fields}
    for node, index in fulltext.items():
        assert index.fields == node.fulltext_fields
        assert index.options["indexConfig"]["fulltext.analyzer"] == "cjk"
    for node in GRAPH_SCHEMA.nodes:
        if node.embedding_fields:
            assert any(index.node is node and index.fields == ("workspace_id", "owner_id")
                       for index in catalog.indexes)
    with pytest.raises(ValueError, match="canonical"):
        pure.neo_catalog.Neo4jConstraint("bad", ENTITY, (replace(ENTITY.property("key")),))
    with pytest.raises(ValueError, match="lacks key constraints"):
        replace(catalog, constraints=catalog.constraints[:-1])
    with pytest.raises(ValueError, match="unique"):
        replace(catalog, indexes=(catalog.indexes[0], catalog.indexes[0]))


def test_neo4j_schema_initialization_waits_and_propagates_index_failure(pure):
    # Ported from infrastructure/storage/test_schema_initialization.py.
    writes = []

    def write(statement, _params, *, operation):
        writes.append(statement)
        return ()

    def read(statement, params, *, operation):
        assert "db.awaitIndex" in statement
        assert "cm_entity_key" in params["index_names"]
        assert operation == "await_schema_online"
        raise RuntimeError("index population failed")

    with pytest.raises(RuntimeError, match="index population failed"):
        pure.neo_schema.ensure_schema(SimpleNamespace(write=write, read=read))
    assert any(statement.startswith("CREATE CONSTRAINT") for statement in writes)
    assert any(statement.startswith("CREATE FULLTEXT INDEX") for statement in writes)
    for timeout in (True, 0, 601):
        with pytest.raises(ValueError):
            pure.neo_schema.ensure_schema(SimpleNamespace(write=write, read=read), timeout_seconds=timeout)


def test_holo_catalog_and_ddl_preserve_native_labels_projections_and_readiness(pure):
    catalog = pure.holo_schema.build_physical_schema(embed_dim=3)
    assert catalog.schema is GRAPH_SCHEMA
    assert catalog.vertex_labels == tuple(node.name for node in GRAPH_SCHEMA.nodes)
    assert catalog.edge_labels == tuple(dict.fromkeys(edge.relationship for edge in GRAPH_SCHEMA.edges))
    statements = []

    def execute(statement):
        statements.append(statement)
        return []

    owner = 'cm:user-space:["tenant-1","workspace-1","user-1"]'
    pure.holo_schema.ensure_schema("personal_graph", catalog, execute, user_space=owner)
    assert any("hg_age_create_vlabel" in query and "UndoBatch" in query for query in statements)
    assert any("ADD COLUMN is_core boolean" in query for query in statements)
    assert any("USING FULLTEXT (content)" in query for query in statements)
    assert any("array_length(plot_embedding, 1) = 3" in query for query in statements)
    assert any("HGraph" in query and "Cosine" in query for query in statements)
    assert not any("UNIQUE" in query for query in statements)
    comments = [query for query in statements if query.startswith("COMMENT ON SCHEMA")]
    assert "initializing:" + owner in comments[0]
    assert owner in statements[-1] and "initializing:" not in statements[-1]
    for dimension in (True, 0, -1):
        with pytest.raises(ValueError):
            pure.holo_schema.build_physical_schema(embed_dim=dimension)


@pytest.mark.parametrize("graph", ["", " leading", "trailing ", "a\nname", "a\x00name", "a\x7fname", "界" * 43])
def test_holo_targets_reject_truncation_and_control_characters(pure, graph):
    with pytest.raises((TypeError, ValueError)):
        pure.holo_target.HoloTarget("tenant", "workspace", graph)


def test_holo_target_preserves_quoted_names_and_reserved_workspace_guards(pure):
    assert pure.holo_target.HoloTarget("tenant", "workspace", 'quoted"graph').graph_name == 'quoted"graph'
    assert pure.holo_target.validate_graph_name("界" * 42) == "界" * 42
    for workspace, name in (("__offline__", None), ("workspace", "cm_graph"), ("workspace", "reserved")):
        with pytest.raises(ValueError):
            pure.holo_target.provisionable_graph_name(workspace, graph_name=name,
                                                     reserved_graph_names=("reserved",))


@pytest.mark.parametrize("native_code,expected,retryable", [
    ("Neo.ClientError.Statement.SyntaxError", "invalid_query", False),
    ("Neo.ClientError.Security.Forbidden", "permission_denied", False),
    ("Neo.TransientError.Transaction.DeadlockDetected", "unavailable", True),
    ("Neo.DatabaseError.Statement.ExecutionFailed", "backend_error", False),
])
def test_neo4j_native_diagnostics_preserve_primary_message_without_driver_context(pure, native_code, expected, retryable):
    # Ported from retrieval/graph/tools/test_tools.py, excluding MCP integration.
    from neo4j.exceptions import Neo4jError
    native = Neo4jError._hydrate_neo4j(code=native_code, message="submitted query failed")
    native.__cause__ = RuntimeError("connection password=secret")
    failure = pure.neo_runtime.Neo4jRuntime._error(native, "execute_query")
    assert (failure.code, failure.retryable) == (expected, retryable)
    assert failure.query_diagnostic.code == native_code
    assert failure.query_diagnostic.message == "submitted query failed"
    assert "secret" not in str(failure) + repr(failure.query_diagnostic)
    assert pure.neo_runtime.Neo4jRuntime._error(native, "get_nodes").query_diagnostic is None


def test_neo4j_errors_distinguish_constraint_index_unavailable_and_unknown_commit(pure):
    from neo4j.exceptions import IncompleteCommit, Neo4jError, ServiceUnavailable
    classify = pure.neo_runtime.Neo4jRuntime._error
    constraint = Neo4jError._hydrate_neo4j(code="Neo.ClientError.Schema.ConstraintValidationFailed", message="constraint")
    assert classify(constraint, "create_nodes").code == "conflict"
    assert classify(constraint, "delete_nodes").code == "constraint_violation"
    missing = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Procedure.ProcedureCallFailed",
        message="Failed to invoke procedure `db.index.fulltext.queryNodes`: Caused by: java.lang.IllegalArgumentException: There is no such fulltext schema index: missing",
    )
    assert classify(missing, "search_fulltext").code == "index_unavailable"
    unavailable = classify(ServiceUnavailable("password=secret"), "execute_query")
    assert unavailable.code == "unavailable" and unavailable.query_diagnostic is None
    uncertain = classify(IncompleteCommit("lost connection"), "atomic")
    assert uncertain.code == "commit_unknown" and not uncertain.retryable


@pytest.mark.parametrize("query_type", ["w", "rw", "s"])
def test_neo4j_native_read_gate_rejects_mutation_before_execution(pure, query_type):
    tx = Mock()
    tx.run.return_value.consume.return_value.query_type = query_type
    with pytest.raises(pure.models.StoreError) as error:
        pure.neo_runtime._run(tx, "MATCH (n) DELETE n", {}, read_only=True)
    assert error.value.code == "cypher_write_forbidden"
    tx.run.assert_called_once_with("EXPLAIN MATCH (n) DELETE n")


def test_neo4j_native_read_gate_detaches_bounded_results(pure):
    plan = SimpleNamespace(consume=lambda: SimpleNamespace(query_type="r"))
    tx = Mock(run=Mock(side_effect=[plan, iter([{"value": 1}, {"value": 2}, {"value": 3}])]))
    assert pure.neo_runtime._run(tx, "RETURN $value", {"value": 1}, read_only=True, max_rows=2) == (
        {"value": 1}, {"value": 2},
    )
    assert tx.run.call_args_list[0].args == ("EXPLAIN RETURN $value",)


class _ManagedSession:
    def __init__(self, transaction):
        self.transaction = transaction
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute_write(self, callback):
        try:
            result = callback(self.transaction)
        except BaseException:
            self.rolled_back = True
            raise
        self.committed = True
        return result

    def execute_read(self, callback):
        return callback(self.transaction)


@pytest.mark.parametrize("failure", ["backend", "validation"])
def test_caught_write_failure_keeps_managed_transaction_uncommittable(pure, failure):
    from neo4j.exceptions import Neo4jError
    native = Neo4jError._hydrate_neo4j(code="Neo.ClientError.Schema.ConstraintValidationFailed", message="duplicate")
    tx = Mock(run=Mock(side_effect=native))
    session = _ManagedSession(tx)
    driver = Mock(session=Mock(return_value=session))
    runtime = pure.neo_runtime.Neo4jRuntime(driver, pure.neo_target.GraphTarget("tenant", "workspace", "graph"))
    borrowed = []

    def work(bound):
        borrowed.append(bound)
        if failure == "backend":
            try:
                bound.write("CREATE (n)", {}, operation="create_nodes")
            except pure.models.StoreError:
                pass
        else:
            bound.fail_transaction()
        return "caught"

    with pytest.raises(pure.models.StoreError) as error:
        runtime.write_work(work, operation="atomic")
    assert error.value.code == "transaction_failed"
    assert session.rolled_back and not session.committed
    with pytest.raises(pure.models.StoreError, match="closed"):
        borrowed[0].read("RETURN 1", {}, operation="get_nodes")
    driver.session.assert_called_once_with(database="graph")


def test_runtime_native_read_timeout_and_driver_close_are_enforced(pure):
    plan = SimpleNamespace(consume=lambda: SimpleNamespace(query_type="r"))
    tx = Mock(run=Mock(side_effect=[plan, iter([{"value": 1}])]))
    session = _ManagedSession(tx)
    callback = []
    original = session.execute_read
    session.execute_read = lambda work: (callback.append(work), original(work))[1]
    driver = Mock(session=Mock(return_value=session))
    runtime = pure.neo_runtime.Neo4jRuntime(driver, pure.neo_target.GraphTarget("tenant", "workspace"))
    assert runtime.read("RETURN 1", {}, operation="execute_query") == ({"value": 1},)
    assert callback[0].timeout == 10
    runtime.close()
    runtime.close()
    driver.close.assert_called_once()
    with pytest.raises(pure.models.StoreError, match="closed"):
        runtime.write("CREATE (n)", {}, operation="create_nodes")


@pytest.mark.parametrize("error_name,operation,expected,retryable", [
    ("UndefinedTable", "execute_query", "invalid_query", False),
    ("InsufficientPrivilege", "execute_query", "permission_denied", False),
    ("SerializationFailure", "execute_query", "unavailable", True),
    ("QueryCanceled", "execute_query", "unavailable", False),
    ("FeatureNotSupported", "execute_query", "backend_error", False),
    ("UniqueViolation", "create_nodes", "conflict", False),
    ("NotNullViolation", "create_edges", "missing_endpoint", False),
])
def test_holo_sqlstate_diagnostics_and_backend_errors(pure, error_name, operation, expected, retryable):
    import psycopg
    from psycopg.pq import DiagnosticField
    runtime = _load("holoage.runtime").HoloRuntime
    native = getattr(psycopg.errors, error_name)("connection password=secret", info={
        DiagnosticField.MESSAGE_PRIMARY: b"submitted query failed",
        DiagnosticField.MESSAGE_DETAIL: b"parameters: password=secret",
        DiagnosticField.CONTEXT: b"connection password=secret",
        DiagnosticField.STATEMENT_POSITION: b"8",
    })
    failure = runtime._error(native, operation)
    assert (failure.code, failure.retryable) == (expected, retryable)
    if operation == "execute_query":
        assert failure.query_diagnostic.code == native.sqlstate
        assert failure.query_diagnostic.message == "submitted query failed"
        assert failure.query_diagnostic.position == 8
    else:
        assert failure.query_diagnostic is None
    assert "secret" not in str(failure) + repr(failure.query_diagnostic)


def test_holo_lost_commit_is_not_retryable(pure):
    import psycopg
    classify = _load("holoage.runtime").HoloRuntime._error
    assert classify(psycopg.OperationalError("lost connection"), "atomic", committing=True).code == "commit_unknown"
    assert not classify(psycopg.OperationalError("lost connection"), "atomic", committing=True).retryable
    assert classify(psycopg.OperationalError("lost connection"), "execute_query").query_diagnostic is None


@pytest.fixture
def authenticated_store():
    try:
        auth = importlib.import_module("context_manager.auth")
        neo = importlib.import_module("context_manager.store.neo4j")
        authorization = importlib.import_module("qwenpaw_data_auth.authorization")
    except ModuleNotFoundError as exc:
        pytest.skip(f"Adjacent Auth/Store integration dependency unavailable: {exc}")
    context = authorization.AuthorizationContext(
        user_id="user-1", tenant_id="tenant-1", role="member",
        active_workspace_role="member", workspace_ids=frozenset({"workspace-1"}),
        active_workspace_id="workspace-1",
    )
    access = auth.GraphScopeAuthority()
    engine = create_engine("sqlite://")
    workspace_glossaries.create(engine)
    glossary = Glossary(GRAPH_SCHEMA, engine, tenant_id="tenant-1", workspace_id="workspace-1")
    runtime = SimpleNamespace(target=neo.GraphTarget("tenant-1", "workspace-1"),
                              read=Mock(return_value=()), write=Mock(return_value=()),
                              fail_transaction=Mock())
    store = neo.Neo4jGraphStore(runtime, GRAPH_SCHEMA, access, glossary, owns_runtime=False)
    yield SimpleNamespace(store=store, runtime=runtime, auth=auth, context=context,
                          read=access.issue_read_scope(context, owner_id="user-1"),
                          write=access.issue_write_scope(context, owner=auth.WriteOwner.USER))
    engine.dispose()


@pytest.mark.parametrize("failure", ["missing_endpoint", "caught_validation"])
def test_atomic_store_rolls_back_preceding_create_after_failed_primitive(authenticated_store, failure):
    # Preserve the source batch-integration rollback contract with an SDK fake.
    fixture = authenticated_store
    neo = importlib.import_module("context_manager.store.neo4j")
    errors = importlib.import_module("context_manager.store")
    tx = Mock(run=Mock(side_effect=[iter([{"index": 0, "key": "ent:user-1:hash-a"}]), iter(())]))
    session = _ManagedSession(tx)
    runtime = neo.Neo4jRuntime(Mock(session=Mock(return_value=session)), fixture.runtime.target)
    store = neo.Neo4jGraphStore(runtime, GRAPH_SCHEMA, fixture.store.access,
                                fixture.store._glossary, owns_runtime=False)
    edge = GRAPH_SCHEMA.edge(("Entity", "ASSOCIATION", "Entity"))

    def work(bound):
        keys = bound.create_nodes(ENTITY, ({"identity_hash": "hash-a", "type": "product"},),
                                  scope=fixture.write)
        assert keys == ("ent:user-1:hash-a",)
        if failure == "missing_endpoint":
            bound.create_edges(edge, ((keys[0], "missing", {}),), scope=fixture.write)
        else:
            try:
                bound.delete_nodes(ENTITY, (keys[0], keys[0]), scope=fixture.write)
            except ValueError:
                pass

    with pytest.raises(errors.StoreError) as error:
        store.atomic(work)
    assert error.value.code == "transaction_failed"
    assert session.rolled_back and not session.committed
    if failure == "missing_endpoint":
        assert error.value.__cause__.code == "missing_endpoint"


def test_batch_creates_return_input_order_and_reject_duplicate_logical_keys(authenticated_store):
    fixture = authenticated_store
    fixture.runtime.write.return_value = (
        {"index": 1, "key": "ent:user-1:hash-b"},
        {"index": 0, "key": "ent:user-1:hash-a"},
    )
    inputs = ({"identity_hash": "hash-a", "type": "product"},
              {"identity_hash": "hash-b", "type": "company"})
    assert fixture.store.create_nodes(ENTITY, inputs, scope=fixture.write) == (
        "ent:user-1:hash-a", "ent:user-1:hash-b",
    )
    rows = fixture.runtime.write.call_args.args[1]["rows"]
    assert rows[0]["core"] is True and rows[1]["core"] is False
    assert rows[0]["properties"]["owner_id"] == "user-1"
    fixture.runtime.write.reset_mock()
    with pytest.raises(ValueError, match="repeated"):
        fixture.store.create_nodes(ENTITY, (inputs[0], inputs[0]), scope=fixture.write)
    fixture.runtime.write.assert_not_called()


@pytest.mark.parametrize("embedding", [(), (0.0,) * 1024, (True,) * 1024,
                                       (float("nan"),) * 1024, (1.0, 0.0)])
def test_vector_validation_rejects_empty_zero_nonfinite_boolean_and_wrong_dimension(authenticated_store, embedding):
    fixture = authenticated_store
    with pytest.raises((TypeError, ValueError)):
        fixture.store.search_vector(embedding, specs=(ENTITY,), scope=fixture.read)
    fixture.runtime.read.assert_not_called()


def test_datasource_scope_is_enforced_before_projection_and_cannot_be_patched(authenticated_store):
    fixture = authenticated_store
    node = GRAPH_SCHEMA.node("Dataset")
    scope = fixture.store.access.issue_read_scope(fixture.context, datasource_id="ds-1")
    fixture.store.get_nodes(node, ("dataset-1",), fields=(), scope=scope)
    query, params = fixture.runtime.read.call_args.args
    assert "n.datasource_id = $n_scope_datasource_id" in query
    assert params["n_scope_datasource_id"] == "ds-1"
    assert "n{.key}" in query
    with pytest.raises(PermissionError):
        fixture.store.patch_nodes(node, (("dataset-1", {"datasource_id": "ds-2"}),), scope=fixture.write)
    fixture.runtime.write.assert_not_called()


def test_normal_store_exports_canonical_public_contract(authenticated_store):
    store = importlib.import_module("context_manager.store")
    assert all(hasattr(store, name) for name in (
        "GraphStore", "NeighborDirection", "NodeDraft", "NodeRecord", "EdgeDraft",
        "EdgeRecord", "GraphPropertyMap", "SubgraphRecord", "StoreError",
    ))


def test_batch_reads_retain_order_missing_entries_repetitions_and_identity(authenticated_store):
    fixture = authenticated_store
    fixture.runtime.read.return_value = (
        {"index": 2, "properties": {"key": "e1"}},
        {"index": 0, "properties": {"key": "e1"}},
    )
    records = fixture.store.get_nodes(ENTITY, ("e1", "missing", "e1"), fields=(), scope=fixture.read)
    assert [record.key if record else None for record in records] == ["e1", None, "e1"]
    query, params = fixture.runtime.read.call_args.args
    assert "n{.key}" in query
    assert params["n_scope_workspace_id"] == "workspace-1"
    assert "user-1" in params["n_scope_owner_ids"]
    assert len(params["rows"]) == 3


def test_empty_batches_and_repeated_write_targets_have_full_contract(authenticated_store):
    fixture = authenticated_store
    assert fixture.store.get_nodes(ENTITY, (), scope=fixture.read) == ()
    assert fixture.store.create_nodes(ENTITY, (), scope=fixture.write) == ()
    assert fixture.store.patch_nodes(ENTITY, (), scope=fixture.write) is None
    assert fixture.store.delete_nodes(ENTITY, (), scope=fixture.write) is None
    fixture.runtime.read.assert_not_called()
    fixture.runtime.write.assert_not_called()
    with pytest.raises(ValueError, match="repeated write target"):
        fixture.store.delete_nodes(ENTITY, ("e1", "e1"), scope=fixture.write)
    fixture.runtime.fail_transaction.assert_called_once()
    fixture.runtime.write.assert_not_called()


def test_store_rejects_foreign_authority_target_zone_and_managed_property_writes(authenticated_store):
    fixture = authenticated_store
    foreign = fixture.auth.GraphScopeAuthority().issue_read_scope(fixture.context)
    with pytest.raises(PermissionError):
        fixture.store.get_nodes(ENTITY, (), scope=foreign)
    other = replace(fixture.context, active_workspace_id="workspace-2", workspace_ids=frozenset({"workspace-2"}))
    with pytest.raises(PermissionError, match="target"):
        fixture.store.get_nodes(ENTITY, (), scope=fixture.store.access.issue_read_scope(other))
    with pytest.raises(PermissionError, match="zone"):
        fixture.store.get_nodes(ENTITY, (), scope=fixture.store.access.issue_read_scope(fixture.context, zone="metadata"))
    with pytest.raises(PermissionError):
        fixture.store.patch_nodes(ENTITY, (("e1", {"owner_id": "user-2"}),), scope=fixture.write)
    fixture.runtime.read.assert_not_called()
    fixture.runtime.write.assert_not_called()


def test_missing_edge_endpoint_is_detected_before_commit(authenticated_store):
    fixture = authenticated_store
    errors = importlib.import_module("context_manager.store")
    edge = GRAPH_SCHEMA.edge(("Entity", "ASSOCIATION", "Entity"))
    fixture.runtime.write.return_value = ()
    with pytest.raises(errors.StoreError) as error:
        fixture.store.create_edges(edge, (("e1", "missing", {}),), scope=fixture.write)
    assert error.value.code == "missing_endpoint"
    fixture.runtime.fail_transaction.assert_called_once()
    query = fixture.runtime.write.call_args.args[0]
    assert "s.owner_id" in query and "t.owner_id" in query


def test_vector_scope_precedes_ranking_and_limit_and_fulltext_treats_operators_literally(authenticated_store):
    fixture = authenticated_store
    vector = (1.0,) * 1024
    fixture.store.search_vector(vector, specs=(ENTITY,), limit=1, scope=fixture.read)
    query, params = fixture.runtime.read.call_args.args
    assert query.index("WHERE") < query.index("vector.similarity.cosine") < query.index("LIMIT")
    assert "db.index.vector.queryNodes" not in query
    assert params["node_scope_workspace_id"] == "workspace-1"
    fixture.store.search_fulltext("AND OR NOT + Qwen*", specs=(ENTITY,), scope=fixture.read)
    assert fixture.runtime.read.call_args.args[1]["query_text"] == '"AND" OR "OR" OR "NOT" OR Qwen'


@pytest.mark.parametrize("rows,code", [
    ([], "user_space_uninitialized"), ([{"owner": None}], "user_space_uninitialized"),
    ([{"owner": 'initializing:cm:user-space:["tenant-1","workspace-1","user-1"]'}], "user_space_initializing"),
    ([{"owner": 'cm:user-space:["tenant-1","workspace-1","user-1"]'}], None),
])
def test_holo_personal_graph_readiness_is_persisted_not_assumed(authenticated_store, rows, code):
    # Ported from storage/holoage/test_user_space.py without MetadataSyncService.
    fixture = authenticated_store
    try:
        holo = importlib.import_module("context_manager.store.holoage")
    except ModuleNotFoundError as exc:
        pytest.skip(f"Real Holo dependency unavailable: {exc}")
    runtime = SimpleNamespace(target=holo.HoloTarget("tenant-1", "workspace-1", "global"),
                              read=Mock(return_value=rows))
    store = holo.HoloGraphStore(runtime, GRAPH_SCHEMA, fixture.store.access, fixture.store._glossary,
                                owns_runtime=False)
    errors = importlib.import_module("context_manager.store")
    if code:
        with pytest.raises(errors.StoreError) as error:
            store._graph_name("user-1")
        assert error.value.code == code
    else:
        assert store._graph_name("user-1") == "tenant-1_workspace-1_user-1"


@pytest.mark.parametrize("marker", [
    'cm:user-space:["tenant-1","workspace-1","user-2"]',
    'initializing:cm:user-space:["tenant-1","workspace-1","user-2"]',
])
def test_holo_wrong_owner_is_forbidden_not_uninitialized(authenticated_store, marker):
    fixture = authenticated_store
    try:
        holo = importlib.import_module("context_manager.store.holoage")
    except ModuleNotFoundError as exc:
        pytest.skip(f"Real Holo dependency unavailable: {exc}")
    runtime = SimpleNamespace(target=holo.HoloTarget("tenant-1", "workspace-1", "global"),
                              read=Mock(return_value=[{"owner": marker}]))
    store = holo.HoloGraphStore(runtime, GRAPH_SCHEMA, fixture.store.access, fixture.store._glossary,
                                owns_runtime=False)
    with pytest.raises(PermissionError):
        store._graph_name("user-1")
