"""Test atomic plans and leased recovery using real Auth, Schema and Store.

Only the native graph transport is in-memory; production Store validation,
prepared scoped Cypher, plan execution, Undo and SQL receipts run unchanged.
"""

import asyncio
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from threading import Event, RLock
from types import SimpleNamespace

import pytest
from qwenpaw_data_auth.authorization import AuthorizationContext
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

import context_manager.transaction.recovery.repository as repository_module
import context_manager.transaction.recovery.stacked as stacked_module
from context_manager.auth import GLOBAL_OWNER_ID, GraphScopeAuthority, WriteOwner
from context_manager.executor import BoundedExecutor
from context_manager.schema import ASSOCIATION, ENTITY, GRAPH_SCHEMA, RECOVERY_STACK_HEAD, UNDO_BATCH, Glossary
from context_manager.schema.relational import metadata, recovery_runs, workspace_glossaries
from context_manager.settings import Settings
from context_manager.settings.execution import PoolSettings
from context_manager.store import EdgeDraft, NodeDraft, StoreError
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from context_manager.store.runtime import GraphRuntime
from context_manager.transaction import StoreOperation, Transaction, TransactionPlan, execute_transaction_plan
from context_manager.transaction.recovery import RecoveryService, RecoveryStackSession
from context_manager.transaction.recovery.codec import decode, encode
from context_manager.transaction.recovery.repository import RecoveryRunRepository
from context_manager.transaction.recovery.stack import RecoveryStack
from context_manager.transaction.recovery.stacked import StackedTransaction


class MemoryTransport(GraphRuntime):
    """Model native CRUD result/commit semantics, not application Store methods."""

    def __init__(self, target, *, nodes=None, edges=None, bound=False):
        super().__init__(target)
        self.nodes = nodes or {}
        self.edges = edges or {}
        self.bound = bound
        self.failed = False
        self.retry_once = False
        self.before = None
        self.calls = []
        self._lock = RLock()

    def fail_transaction(self):
        self.failed = True

    def write_work(self, work, *, operation):
        assert not self.bound
        with self._lock:
            attempts = 2 if self.retry_once else 1
            self.retry_once = False
            for attempt in range(attempts):
                tx = MemoryTransport(self.target, nodes=deepcopy(self.nodes), edges=deepcopy(self.edges), bound=True)
                tx.before = self.before
                tx.calls = self.calls
                try:
                    result = work(tx)
                    if tx.failed:
                        raise StoreError("transaction failed", operation=operation, code="transaction_failed")
                    if attempt + 1 == attempts:
                        self.nodes, self.edges = tx.nodes, tx.edges
                        return result
                finally:
                    tx.close()

    @staticmethod
    def visible(query, parameters, alias, properties):
        if properties is None:
            return False
        for field, operator, parameter in re.findall(rf"\b{alias}\.(\w+) (IN|=) \$(\w+)", query):
            expected = parameters[parameter]
            actual = properties.get(field)
            if (operator == "IN" and actual not in expected) or (operator == "=" and actual != expected):
                return False
        return all(properties.get(field) is None for field in re.findall(rf"\b{alias}\.(\w+) IS NULL", query))

    @staticmethod
    def project(query, alias, properties):
        match = re.search(rf"\b{alias}\{{([^}}]*)\}}", query)
        if match is None:
            return deepcopy(properties)
        fields = re.findall(r"\.(\w+)", match[1])
        return {name: deepcopy(properties.get(name)) for name in fields}

    def _execute(self, query, parameters, *, operation, write, max_rows=None):
        assert not self.failed
        self.calls.append((operation, query, deepcopy(parameters)))
        if self.before is not None:
            self.before(operation, query)
        rows = []
        if operation.endswith("_edges"):
            source = re.search(r"\(s:(\w+)\)", query)[1]
            target = re.search(r"\(t:(\w+)\)", query)[1]
            relation = re.search(r"\[r:(\w+)\]", query)[1]
            for item in parameters["rows"]:
                left, right = item["source_key"], item["target_key"]
                identity = (source, relation, target, left, right)
                endpoints = self.visible(query, parameters, "s", self.nodes.get((source, left))) and self.visible(
                    query, parameters, "t", self.nodes.get((target, right)))
                if not endpoints:
                    continue
                if operation == "create_edges":
                    if identity in self.edges:
                        raise ValueError("duplicate edge")
                    self.edges[identity] = deepcopy(item["properties"])
                record = self.edges.get(identity)
                if not self.visible(query, parameters, "r", record):
                    continue
                if operation == "patch_edges":
                    self._patch(record, item["changes"])
                elif operation == "delete_edges":
                    del self.edges[identity]
                else:
                    rows.append({"index": item["index"], "source_key": left, "target_key": right,
                                 "properties": self.project(query, "r", record)})
            return tuple(rows)
        label = re.search(r"\(n:(\w+)\)", query)[1]
        if operation == "list_nodes":
            records = sorted((value for (kind, _), value in self.nodes.items() if kind == label), key=lambda value: value["key"])
            visible = [value for value in records if self.visible(query, parameters, "n", value)]
            return tuple({"properties": self.project(query, "n", value)} for value in
                         visible[parameters["offset"]:parameters["offset"] + parameters["limit"]])
        for item in parameters["rows"]:
            if operation == "create_nodes":
                record = deepcopy(item["properties"])
                identity = (label, record["key"])
                if identity in self.nodes:
                    raise ValueError("duplicate node")
                self.nodes[identity] = record
                rows.append({"index": item["index"], "key": record["key"]})
                continue
            identity = (label, item["key"])
            record = self.nodes.get(identity)
            if not self.visible(query, parameters, "n", record):
                continue
            if operation == "patch_nodes":
                self._patch(record, item["changes"])
            elif operation == "delete_nodes":
                if any(identity[1] in edge[-2:] for edge in self.edges):
                    raise ValueError("node still has edges")
                del self.nodes[identity]
            else:
                assert operation == "get_nodes"
                rows.append({"index": item["index"], "properties": self.project(query, "n", record)})
        return tuple(rows)

    @staticmethod
    def _patch(properties, changes):
        for name, value in changes.items():
            if value is None:
                properties.pop(name, None)
            else:
                properties[name] = deepcopy(value)


class MemoryTransaction:
    """Implement the SDK's run boundary for the real Neo4jRuntime."""

    def __init__(self, native):
        self.native = native

    def run(self, query, **parameters):
        if "CREATE (n:" in query:
            operation = "create_nodes"
        elif "CREATE (s)-[r:" in query:
            operation = "create_edges"
        elif "SET n +=" in query:
            operation = "patch_nodes"
        elif "SET r +=" in query:
            operation = "patch_edges"
        elif "DELETE n" in query:
            operation = "delete_nodes"
        elif "DELETE r" in query:
            operation = "delete_edges"
        elif "ORDER BY n.key" in query:
            operation = "list_nodes"
        else:
            operation = "get_edges" if "[r:" in query else "get_nodes"
        return self.native._execute(query, parameters, operation=operation,
                                    write=operation.startswith(("create_", "patch_", "delete_")))


class MemorySession:
    def __init__(self, native):
        self.native = native

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        return False

    def execute_write(self, callback):
        return self.native.write_work(lambda tx: callback(MemoryTransaction(tx)), operation="atomic")

    def execute_read(self, callback):
        return callback(MemoryTransaction(self.native))


class MemoryDriver:
    def __init__(self, native):
        self.native = native

    def session(self, *, database):
        assert database == "graph"
        return MemorySession(self.native)

    def close(self):
        self.native.close()


def context(user="alice", **changes):
    fields = dict(user_id=user, tenant_id="tenant", role="ws_admin", active_workspace_role="ws_admin",
                  workspace_ids=frozenset({"workspace"}), admin_workspace_ids=frozenset({"workspace"}),
                  active_workspace_id="workspace")
    return AuthorizationContext(**(fields | changes))


@pytest.fixture
def graph(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(engine, tables=[workspace_glossaries, recovery_runs])
    settings = Settings(environment={})
    monkeypatch.setattr(repository_module, "get_settings", lambda: settings)
    monkeypatch.setattr(repository_module, "current_pod_name", lambda: "test-pod")
    monkeypatch.setattr("context_manager.transaction.recovery.session.current_pod_name", lambda: "test-pod")
    authority = GraphScopeAuthority()
    native = MemoryTransport(GraphTarget("tenant", "workspace", "graph"))
    glossary = Glossary(GRAPH_SCHEMA, engine, tenant_id="tenant", workspace_id="workspace")
    runtime = Neo4jRuntime(MemoryDriver(native), native.target)
    store = Neo4jGraphStore(runtime, GRAPH_SCHEMA, authority, glossary, owns_runtime=False)
    write = authority.issue_write_scope(context(), owner=WriteOwner.USER)
    system = context("__system__")

    def stack(rid):
        return RecoveryStack(store, rid, control_scope=authority.issue_admin_scope(system, owner_id=GLOBAL_OWNER_ID, zone="_internal"),
                             read_scope=authority.issue_admin_scope(system), scope_for=lambda owner, datasource:
                             authority.issue_admin_scope(system, owner_id=owner, datasource_id=datasource))

    yield SimpleNamespace(store=store, native=native, access=authority, scope=write, stack=stack,
                          engine=engine, runs=RecoveryRunRepository(engine), settings=settings)
    native.close()
    engine.dispose()


@pytest.fixture
async def executor():
    worker = BoundedExecutor("focused-recovery", PoolSettings(workers=2, max_waiters=8, queue_timeout_seconds=2))
    yield worker
    await worker.aclose()


def identity(task="task", rid="run", **changes):
    return dict(tenant_id="tenant", workspace_id="workspace", task_type="knowledge", task_id=task, rid=rid, **changes)


def create_plan(*refs):
    return TransactionPlan((StoreOperation.project_nodes(tuple(NodeDraft.build(
        GRAPH_SCHEMA, ENTITY, ref, {"identity_hash": ref, "type": "product", "name": ref, "source_ids": ["document-1"]},
        transient={"record_attribution": "document-1"}) for ref in refs)),))


def patch_plan(key, **properties):
    return TransactionPlan((StoreOperation("patch_nodes", {"spec": ENTITY, "patches": ((key, properties),)}),))


def test_plan_rejects_nonprimitives_unknown_arguments_and_forward_refs():
    for name in ("atomic", "health", "__getattribute__"):
        with pytest.raises(ValueError, match="unsupported"):
            StoreOperation(name, {})
    with pytest.raises(ValueError, match="missing"):
        StoreOperation("create_nodes", {"spec": ENTITY})
    with pytest.raises(ValueError, match="unsupported parameters"):
        StoreOperation("create_nodes", {"spec": ENTITY, "properties": (), "tenant_id": "other"})
    operation = StoreOperation("get_nodes", {"spec": ENTITY, "keys": (None,)}, {("keys", 0): "forward"})
    with pytest.raises(ValueError, match="unavailable"):
        TransactionPlan((operation,))
    with pytest.raises(ValueError, match="unique"):
        TransactionPlan(create_plan("same").operations * 2)
    with pytest.raises(ValueError, match="placeholder"):
        StoreOperation("get_nodes", {"spec": ENTITY, "keys": ("not-empty",)}, {("keys", 0): "ref"})
    mutable = {"spec": ENTITY, "properties": [{"type": "product", "identity_hash": "hash"}]}
    frozen = StoreOperation("create_nodes", mutable)
    mutable["properties"][0]["type"] = "changed"
    assert frozen.parameters["properties"][0]["type"] == "product"
    with pytest.raises(TypeError):
        frozen.parameters["properties"][0]["type"] = "changed"


def test_ordinary_plan_is_one_atomic_scoped_commit_with_refs_stats_and_attribution(graph):
    nodes = create_plan("left", "right").operations[0]
    edge = StoreOperation.project_edges((EdgeDraft.build(GRAPH_SCHEMA, ASSOCIATION, "left", "right", {"description": "linked"}),))
    read = StoreOperation("get_nodes", {"spec": ENTITY, "keys": (None, "missing")}, {("keys", 0): "left"}, node_refs=("loaded", None))
    patch = StoreOperation("patch_nodes", {"spec": ENTITY, "patches": ((None, {"description": "new"}), ("missing", {"description": "ignored"}))},
                           {("patches", 0, 0): "loaded"})
    plan = TransactionPlan((nodes, edge, read, patch))
    result = execute_transaction_plan(graph.store, plan, graph.scope)
    assert result.node_keys["left"] == result.node_keys["loaded"]
    assert result.stats["nodes"] == {"created": 2, "read": 1, "updates_submitted": 2, "deletes_submitted": 0}
    assert result.stats["edges"]["created"] == 1
    key = result.node_keys["left"]
    record = graph.store.get_nodes(ENTITY, (key,), scope=graph.scope)[0]
    assert record.properties["description"] == "new" and record.properties["owner_id"] == "alice"
    assert record.properties["workspace_id"] == "workspace" and record.properties["source_ids"] == ("document-1",)
    assert "record_attribution" not in record.properties
    assert plan.operations[-1].parameters["patches"][0][0] is None
    assert plan.operations[1].parameters["edges"][0][:2] == (None, None)
    with pytest.raises(TypeError):
        result.node_keys["left"] = "changed"
    bob = graph.access.issue_read_scope(context("bob"), owner_id="bob")
    assert graph.store.get_nodes(ENTITY, (key,), scope=bob) == (None,)
    with pytest.raises(ValueError, match="one scope"):
        Transaction(graph.store).execute(plan, (graph.scope,))


@pytest.mark.parametrize("failure", ["validate", "missing", "forged"])
def test_ordinary_plan_rolls_back_every_prior_write_on_failure(graph, failure):
    operations = list(create_plan("left").operations)
    scopes = [graph.scope]
    if failure == "missing":
        operations.append(StoreOperation("create_edges", {"spec": ASSOCIATION, "edges": ((None, "missing", {}),)}, {("edges", 0, 0): "left"}))
        scopes.append(graph.scope)
    elif failure == "forged":
        operations.append(create_plan("right").operations[0])
        scopes.append(GraphScopeAuthority().issue_write_scope(context(), owner=WriteOwner.USER))

    def validate(_result):
        if failure == "validate":
            raise ValueError("rejected before commit")

    with pytest.raises((ValueError, StoreError, PermissionError)):
        Transaction(graph.store).execute(TransactionPlan(tuple(operations)), scopes, validate=validate)
    assert not graph.native.nodes and not graph.native.edges


def test_stacked_retry_keeps_business_and_one_undo_append_atomic(graph):
    stack = graph.stack("retry")
    stack.create()
    graph.native.retry_once = True
    result = StackedTransaction(stack, graph.scope, 1).execute(create_plan("created"), graph.scope)
    assert result.stats["nodes"]["created"] == 1 and stack.head().properties["head_seq"] == 1
    batches = graph.store.list_nodes(UNDO_BATCH, scope=stack.control_scope)
    assert len(batches) == 1 and len(decode(batches[0].properties["payload"])) == 1
    assert decode(batches[0].properties["payload"])[0]["scope"] == {"owner_id": "alice", "datasource_id": None}
    with pytest.raises(StoreError) as error:
        StackedTransaction(stack, graph.scope, 1).execute(create_plan("never"), graph.scope)
    assert error.value.code == "recovery_batch_committed"
    assert len(graph.store.list_nodes(ENTITY, scope=graph.scope)) == 1
    stack.decide("FAILED", "interrupted")
    stack.undo_batch(1)
    stack.undo_batch(1)  # A lost response is safe to retry.
    assert not graph.store.list_nodes(ENTITY, scope=graph.scope)
    assert stack.cleanup_batch() is False
    assert stack.cleanup_batch() is True and stack.cleanup_batch() is True


def test_stacked_append_failure_rolls_back_business_data_and_head(graph):
    stack = graph.stack("append-failure")
    stack.create()

    def fail(operation, query):
        if operation == "create_nodes" and "UndoBatch" in query:
            raise RuntimeError("log storage failed")

    graph.native.before = fail
    with pytest.raises(RuntimeError, match="log storage"):
        StackedTransaction(stack, graph.scope, 1).execute(create_plan("created"), graph.scope)
    graph.native.before = None
    assert stack.head().properties["head_seq"] == 0
    assert not graph.store.list_nodes(ENTITY, scope=graph.scope)


def test_reverse_batches_restore_all_six_crud_writes_exactly(graph):
    initial = Transaction(graph.store).execute(create_plan("left", "right"), graph.scope)
    left, right = initial.node_keys["left"], initial.node_keys["right"]
    Transaction(graph.store).execute(TransactionPlan((StoreOperation("create_edges", {
        "spec": ASSOCIATION, "edges": ((left, right, {"description": "original"}),)}),)), graph.scope)
    before_nodes, before_edges = deepcopy(graph.native.nodes), deepcopy(graph.native.edges)
    stack = graph.stack("all-writes")
    stack.create()
    StackedTransaction(stack, graph.scope, 1).execute(TransactionPlan((
        StoreOperation("patch_nodes", {"spec": ENTITY, "patches": ((left, {"description": "temporary"}),)}),
        StoreOperation("patch_edges", {"spec": ASSOCIATION, "patches": ((left, right, {"description": "temporary"}),)}),
        StoreOperation("delete_edges", {"spec": ASSOCIATION, "endpoints": ((left, right),)}),
        StoreOperation("delete_nodes", {"spec": ENTITY, "keys": (left,)}),
    )), graph.scope)
    plan = create_plan("new-left", "new-right")
    plan = TransactionPlan((*plan.operations, StoreOperation.project_edges((
        EdgeDraft.build(GRAPH_SCHEMA, ASSOCIATION, "new-left", "new-right", {"description": "new"}),))))
    StackedTransaction(stack, graph.scope, 2).execute(plan, graph.scope)
    stack.decide("KILLED", "cancelled")
    with pytest.raises(RuntimeError, match="current stack head"):
        stack.undo_batch(1)
    stack.undo_batch(2)
    stack.undo_batch(1)
    while not stack.cleanup_batch():
        pass
    assert graph.native.nodes == before_nodes and graph.native.edges == before_edges


def test_restore_uses_original_record_owner_not_broad_requester(graph):
    bob = graph.access.issue_admin_scope(context(), owner_id="bob")
    key = Transaction(graph.store).execute(create_plan("bob-node"), bob).node_keys["bob-node"]
    broad = graph.access.issue_admin_scope(context())
    stack = graph.stack("record-owner")
    stack.create()
    StackedTransaction(stack, broad, 1).execute(TransactionPlan((StoreOperation("delete_nodes", {"spec": ENTITY, "keys": (key,)}),)), bob)
    stack.decide("FAILED", None)
    stack.undo_batch(1)
    restored = graph.store.get_nodes(ENTITY, (key,), scope=bob)[0]
    assert restored.properties["owner_id"] == "bob" and restored.properties["source_ids"] == ("document-1",)


@pytest.mark.parametrize("violation", ["read", "internal", "owner", "target", "zone", "budget", "bytes", "duplicate-edge"])
def test_stacked_boundaries_reject_before_business_commit(graph, monkeypatch, violation):
    stack = graph.stack("boundary")
    stack.create()
    plan, scope = create_plan("created"), graph.scope
    if violation == "read":
        plan = TransactionPlan((StoreOperation("get_nodes", {"spec": ENTITY, "keys": ()}),))
    elif violation == "internal":
        plan = TransactionPlan((StoreOperation("delete_nodes", {"spec": RECOVERY_STACK_HEAD, "keys": ()}),))
    elif violation == "owner":
        scope = graph.access.issue_admin_scope(context(), owner_id="bob")
    elif violation == "target":
        scope = graph.access.issue_write_scope(context(tenant_id="other"), owner=WriteOwner.USER)
    elif violation == "zone":
        scope = graph.access.issue_write_scope(context(), owner=WriteOwner.USER, zone="user")
    elif violation == "budget":
        monkeypatch.setattr(stacked_module, "get_settings", lambda: SimpleNamespace(graph=SimpleNamespace(write_batch_max_rows=0)))
    elif violation == "bytes":
        monkeypatch.setattr(stacked_module, "_MAX_BATCH_BYTES", 1)
    elif violation == "duplicate-edge":
        result = Transaction(graph.store).execute(create_plan("left", "right"), graph.scope)
        edge = StoreOperation("create_edges", {"spec": ASSOCIATION, "edges": ((result.node_keys["left"], result.node_keys["right"], {}),)})
        plan = TransactionPlan((edge,))
        Transaction(graph.store).execute(plan, graph.scope)
    before = deepcopy(graph.native.nodes), deepcopy(graph.native.edges)
    with pytest.raises((ValueError, PermissionError)):
        StackedTransaction(stack, graph.scope, 1).execute(plan, scope)
    assert (graph.native.nodes, graph.native.edges) == before
    assert stack.head().properties["head_seq"] == 0


def test_codec_preserves_temporal_null_and_user_mapping_tags():
    values = ({"date": date(2026, 1, 2), "datetime": datetime(2026, 1, 2, tzinfo=UTC),
               "user_json": {"datetime": "ordinary text"}, "missing": None, "list": [1, True]},)
    decoded = decode(encode(values))
    assert decoded[0]["date"] == values[0]["date"] and decoded[0]["datetime"] == values[0]["datetime"]
    assert decoded[0]["user_json"] == {"datetime": "ordinary text"} and decoded[0]["list"] == (1, True)
    with pytest.raises(ValueError):
        encode(float("nan"))
    with pytest.raises(ValueError):
        decode("NaN")
    with pytest.raises(TypeError):
        encode({1: "bad key"})


def test_repository_fences_claim_owner_attempt_and_explicit_retry(graph, monkeypatch):
    runs = graph.runs
    run = identity()
    runs.register(run, live=True)
    assert not runs.has_pending("tenant", "workspace") and runs.discover() == ()
    assert not runs.claim_abandoned(run)
    with pytest.raises(StoreError) as busy:
        runs.register(identity(rid="replacement"))
    assert busy.value.code == "recovery_task_busy"
    with graph.engine.begin() as sql:
        sql.execute(update(recovery_runs).values(heartbeat_at=0, executor_pod="old-pod"))
    assert runs.has_pending("tenant", "workspace") and not runs.has_pending("tenant", "other")
    assert runs.claim_abandoned(run) and not runs.claim_abandoned(run)
    monkeypatch.setattr(repository_module, "current_pod_name", lambda: "old-pod")
    with pytest.raises(StoreError) as fenced:
        runs.heartbeat(run)
    assert fenced.value.code == "recovery_fenced"
    runs.release_if_open(run)
    assert runs.get(run)["executor_pod"] == "test-pod"
    monkeypatch.setattr(repository_module, "current_pod_name", lambda: "test-pod")
    runs.update(run, values={"recovery_state": "CLOSED"}, states=("REGISTERED",), owner="test-pod")
    for previous in (None, "wrong"):
        with pytest.raises(StoreError):
            runs.register(identity(rid="replacement"), previous_rid=previous)
    runs.register(identity(rid="replacement"), previous_rid="run", live=True)
    with pytest.raises(StoreError):
        runs.heartbeat(run)
    assert runs.get(run)["rid"] == "replacement"


async def test_service_commits_promotes_receipt_and_accepts_only_explicit_retry(graph, executor):
    service = RecoveryService(graph.runs, executor, lambda row: graph.stack(row["rid"]))
    try:
        async with service.run(graph.scope, task_type="knowledge", task_id="task", rid="run") as session:
            result = await executor.run(session.execute, create_plan("created"), graph.scope)
            with pytest.raises(StoreError) as busy:
                async with service.run(graph.scope, task_type="knowledge", task_id="task"):
                    pytest.fail("duplicate task admitted")
            assert busy.value.code == "recovery_task_busy"
            session.set_result("DEGRADED", "partial")
        assert session.receipt["recovery_state"] == "CLOSED" and session.receipt["result_status"] == "DEGRADED"
        assert graph.store.get_nodes(ENTITY, (result.node_keys["created"],), scope=graph.scope)[0]
        assert graph.stack("run").head() is None and not service._active
        async with service.run(graph.scope, task_type="knowledge", task_id="task", rid="retry", previous_rid="run"):
            pass
        assert graph.runs.get(identity())["rid"] == "retry"
    finally:
        await service.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        async with service.run(graph.scope, task_type="knowledge"):
            pass


@pytest.mark.parametrize("decision", [None, "SUCCESS"])
async def test_abandoned_scan_undo_or_promote_never_replays_business(graph, executor, decision):
    run = identity()
    graph.runs.register(run)
    stack = graph.stack("run")
    stack.create()
    StackedTransaction(stack, graph.scope, 1).execute(create_plan("created"), graph.scope)
    graph.runs.update(run, values={"recovery_state": "ACTIVE"}, states=("REGISTERED",))
    if decision:
        stack.decide(decision, "committed before response was lost")
    service = RecoveryService(graph.runs, executor, lambda row: graph.stack(row["rid"]))
    try:
        with pytest.raises(StoreError) as busy:
            await service.require_ready(graph.scope)
        assert busy.value.code == "recovery_workspace_busy"
        await service.recover_pending()
        await service.recover_pending()
        row = graph.runs.get(run)
        assert row["recovery_state"] == "CLOSED" and row["result_status"] == (decision or "FAILED")
        assert bool(graph.store.list_nodes(ENTITY, scope=graph.scope)) is bool(decision)
        assert stack.head() is None
        await service.require_ready(graph.scope)
    finally:
        await service.aclose()


@pytest.mark.parametrize("state", ["REGISTERED", "ACTIVE", "CLEANING"])
async def test_recovery_missing_head_distinguishes_unstarted_broken_and_cleaned(graph, executor, state):
    run = identity()
    graph.runs.register(run)
    if state != "REGISTERED":
        graph.runs.update(run, values={"recovery_state": state, "result_status": "SUCCESS" if state == "CLEANING" else None}, states=("REGISTERED",))
    session = RecoveryStackSession(graph.stack("run"), graph.runs, executor, run, None)
    if state == "ACTIVE":
        with pytest.raises(StoreError) as failure:
            await session.recover()
        assert failure.value.code == "recovery_head_missing"
        assert graph.runs.get(run)["heartbeat_at"] is None
    else:
        await session.recover()
        assert session.receipt["recovery_state"] == "CLOSED"
        assert session.receipt["result_status"] == ("SUCCESS" if state == "CLEANING" else "FAILED")


async def test_caught_failed_batch_never_allows_later_success(graph, executor):
    session = RecoveryStackSession(graph.stack("run"), graph.runs, executor, identity(), graph.scope)
    with pytest.raises(RuntimeError, match="failed batch"):
        async with session:
            await executor.run(session.execute, create_plan("created"), graph.scope)
            with pytest.raises(ValueError):
                await executor.run(session.execute, TransactionPlan((StoreOperation("get_nodes", {"spec": ENTITY, "keys": ()}),)), graph.scope)
            with pytest.raises(RuntimeError, match="no longer accepts"):
                await executor.run(session.execute, create_plan("later"), graph.scope)
    assert not graph.store.list_nodes(ENTITY, scope=graph.scope)
    assert session.receipt["result_status"] == "FAILED"


async def test_cancellation_drains_native_batch_then_undo_and_service_shutdown(graph, executor):
    entered, release, completed = Event(), Event(), Event()
    service = RecoveryService(graph.runs, executor, lambda row: graph.stack(row["rid"]))

    def block(operation, query):
        if operation == "create_nodes" and "(n:Entity)" in query:
            entered.set()
            assert release.wait(5)
            completed.set()

    graph.native.before = block

    async def work():
        async with service.run(graph.scope, task_type="knowledge", task_id="task", rid="run") as session:
            await executor.run(session.execute, create_plan("created"), graph.scope)

    task = asyncio.create_task(work())
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        assert not task.done() and not shutdown.done() and service._active
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    await asyncio.wait_for(shutdown, 5)
    assert completed.is_set() and not service._active
    assert graph.runs.get(identity())["result_status"] == "KILLED"
    assert not graph.store.list_nodes(ENTITY, scope=graph.scope) and graph.stack("run").head() is None
