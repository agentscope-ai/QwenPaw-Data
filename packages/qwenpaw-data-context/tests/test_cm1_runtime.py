"""Exercise real runtime routing, pooling, resource closure and loader wiring."""

import ast
import asyncio
import inspect
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from qwenpaw_data_auth.authorization import AuthorizationContext
from sqlalchemy import text

import context_manager.runtime.context as context_module
import context_manager.runtime.resolver as resolver_module
from context_manager.auth import AccessMode, GraphScopeAuthority, GraphScopeError, ScopeBinding, WriteOwner
from context_manager.db import CMDatabase
from context_manager.runtime import ContextRuntime, Neo4jConnectionConfig, WorkspaceResolver, WorkspaceRuntime, WorkspaceRuntimePool
from context_manager.runtime.errors import ContextRuntimeUnavailable
from context_manager.runtime.execution import ExecutionResources
from context_manager.runtime.startup import create_context_runtime, create_service_runtime
from context_manager.settings import Settings, clear_settings
from context_manager.settings.db import DbSettings
from context_manager.settings.execution import ExecutionSettings, PoolSettings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    clear_settings()
    yield
    clear_settings()


def connection(**changes):
    fields = dict(tenant_id="tenant", workspace_id="workspace", neo4j_uri="bolt://localhost:7687",
                  neo4j_user="neo4j", neo4j_password="test-password", neo4j_database="graph")
    fields.update(changes)
    return Neo4jConnectionConfig(**fields)


def identity(**changes):
    fields = dict(user_id="alice", tenant_id="tenant", role="member", active_workspace_role="member",
                  workspace_ids=frozenset({"workspace"}), active_workspace_id="workspace")
    fields.update(changes)
    return AuthorizationContext(**fields)


def workspace_fixture(route, *, events=None, healthy=True):
    """Populate only Store collaborators needed by actual pool/lifecycle methods.

    Avoid inventing adjacent modules or bypassing their real constructors in
    production. These tests do not claim to construct the unported services.
    """
    runtime = object.__new__(WorkspaceRuntime)
    runtime._target = route.target
    runtime._state_lock = Lock()
    runtime._closed = False
    runtime._store = SimpleNamespace(
        access=GraphScopeAuthority(), health=lambda: {"ok": healthy},
        close=lambda: events.append("store") if events is not None else None,
    )
    return runtime


def runtime_fixture(resolver):
    runtime = object.__new__(ContextRuntime)
    runtime._resolver = resolver
    runtime._state_lock = Lock()
    runtime._closed = False
    runtime._shutdown_task = None
    runtime._initializer = None
    runtime._database = None
    runtime._recovery = None
    runtime._usage_recorder = None
    runtime._sweep_stop = None
    runtime._sweep_task = None
    return runtime


def test_connections_hide_credentials_and_validate_identity():
    route = connection()
    assert "test-password" not in repr(route)
    assert (route.target.tenant_id, route.target.workspace_id, route.target.database) == ("tenant", "workspace", "graph")
    for fields in ({"tenant_id": " tenant"}, {"workspace_id": ""}, {"neo4j_password": ""}, {"neo4j_uri": " bolt://localhost"}):
        with pytest.raises(RuntimeError):
            connection(**fields)


@pytest.mark.parametrize("tenant,workspace", [("other", "workspace"), ("tenant", "other")])
def test_fixed_development_routing_fails_closed_before_pool_access(tenant, workspace):
    calls = []
    pool = WorkspaceRuntimePool(lambda route: calls.append(route))
    resolver = WorkspaceResolver(pool, connection=connection(), settings=Settings(environment={}))
    with pytest.raises(PermissionError):
        resolver.resolve(tenant, workspace)
    with pytest.raises(PermissionError):
        resolver.require_workspace(tenant, workspace)
    assert calls == []
    assert resolver.fixed_target == connection().target
    resolver.close()


def test_pool_single_flight_shares_one_healthy_workspace():
    entered, release = Event(), Event()
    calls, events = [], []

    def factory(route):
        calls.append(route)
        entered.set()
        assert release.wait(5)
        return workspace_fixture(route, events=events)

    pool = WorkspaceRuntimePool(factory)
    with ThreadPoolExecutor(max_workers=8) as workers:
        futures = [workers.submit(pool.resolve, connection()) for _ in range(8)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(calls) == 1
    assert all(runtime is results[0] for runtime in results)
    pool.close()
    pool.close()
    assert events == ["store"]
    with pytest.raises(ContextRuntimeUnavailable, match="pool is closed"):
        pool.resolve(connection())


@pytest.mark.parametrize("failure", ["health", "target"])
def test_pool_does_not_publish_failed_or_mismatched_runtime_and_can_retry(failure):
    events, calls = [], []

    def factory(route):
        calls.append(route)
        if len(calls) == 1:
            return workspace_fixture(
                connection(workspace_id="other") if failure == "target" else route,
                events=events, healthy=failure != "health",
            )
        return workspace_fixture(route, events=events)

    pool = WorkspaceRuntimePool(factory)
    with pytest.raises(ContextRuntimeUnavailable):
        pool.resolve(connection())
    assert not pool._runtimes and not pool._initializing
    assert events == ["store"]
    assert pool.resolve(connection()).target == connection().target
    pool.close()
    assert events == ["store", "store"]


def test_pool_closure_during_creation_releases_created_store():
    entered, release = Event(), Event()
    events = []

    def factory(route):
        entered.set()
        assert release.wait(5)
        return workspace_fixture(route, events=events)

    pool = WorkspaceRuntimePool(factory)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(pool.resolve, connection())
        try:
            assert entered.wait(5)
            pool.close()
        finally:
            release.set()
        with pytest.raises(ContextRuntimeUnavailable, match="pool is closed"):
            future.result(timeout=5)
    assert events == ["store"] and not pool._initializing


def test_facade_issues_owner_scopes_and_rejects_forged_scope_before_exposing_store():
    pool = WorkspaceRuntimePool(workspace_fixture)
    runtime = runtime_fixture(WorkspaceResolver(pool, connection=connection()))
    read = runtime.issue_graph_read_scope(identity())
    write = runtime.issue_graph_write_scope(identity(), owner=WriteOwner.USER)
    assert read.owner_ids == write.owner_ids == ("alice",)
    assert runtime.graph_store(read) is runtime.graph_store(write)
    direct = ScopeBinding("tenant", "workspace", "alice", AccessMode.READ)
    for forged in (direct, replace(read, owner_ids=()), GraphScopeAuthority().issue_read_scope(identity())):
        with pytest.raises(GraphScopeError):
            runtime.graph_store(forged)
    with pytest.raises(PermissionError):
        runtime.graph_store(replace(read, workspace_id="other"))
    with pytest.raises(PermissionError):
        runtime.issue_graph_read_scope(identity(tenant_id="other"))
    with pytest.raises(GraphScopeError):
        runtime.graph_store(object())
    pool.close()


def test_closed_facade_will_not_route_or_bind_models():
    pool = WorkspaceRuntimePool(workspace_fixture)
    runtime = runtime_fixture(WorkspaceResolver(pool, connection=connection()))
    runtime._closed = True
    assert runtime.health() == {"ok": False}
    with pytest.raises(RuntimeError, match="runtime is closed"):
        runtime.issue_graph_read_scope(identity())
    with pytest.raises(RuntimeError, match="runtime is closed"):
        runtime.models(identity())
    assert not pool._runtimes
    pool.close()


@pytest.fixture
def registry(monkeypatch):
    from qwenpaw_data_auth.database import metadata, tenants, workspaces

    database = CMDatabase.from_settings(DbSettings(url="sqlite:///:memory:"))
    metadata.create_all(database.engine, tables=[tenants, workspaces])
    with database.engine.begin() as sql:
        sql.execute(tenants.insert(), {"id": "tenant", "name": "Tenant", "neo4j_database": "graph"})
        sql.execute(workspaces.insert(), {
            "id": "workspace", "tenant_id": "tenant", "name": "Workspace",
            "instance_status": "ready", "endpoint_ref": "env://QWENPAW_DATA_GRAPH_ENDPOINT",
            "secret_ref": "env:QWENPAW_DATA_GRAPH_SECRET",
        })
    monkeypatch.setattr(resolver_module, "get_database", lambda: database)
    yield database
    database.close()


def test_registry_routes_exact_tenant_workspace_using_public_secret_references(registry):
    environment = {
        "QWENPAW_DATA_GRAPH_ENDPOINT": "bolt://localhost:7687",
        "QWENPAW_DATA_GRAPH_SECRET": '{"user":"neo4j","password":"test-password"}',
    }
    pool = WorkspaceRuntimePool(workspace_fixture)
    resolver = WorkspaceResolver(pool, settings=Settings(environment=environment))
    assert resolver.fixed_target is None
    assert resolver.health()
    resolved = resolver.resolve("tenant", "workspace")
    assert resolved.target == connection().target
    assert resolver.resolve("tenant", "workspace") is resolved
    with pytest.raises(PermissionError, match="not registered"):
        resolver.resolve("other-tenant", "workspace")
    with pytest.raises(PermissionError, match="not registered"):
        resolver.resolve("tenant", "other-workspace")
    resolver.close()


@pytest.mark.parametrize("target", ["tenant", "workspace", "instance"])
def test_registry_rejects_inactive_or_unready_targets(registry, target):
    with registry.engine.begin() as sql:
        if target == "tenant":
            sql.execute(text("UPDATE tenants SET status='disabled'"))
        elif target == "workspace":
            sql.execute(text("UPDATE workspaces SET status='disabled'"))
        else:
            sql.execute(text("UPDATE workspaces SET instance_status='pending'"))
    pool = WorkspaceRuntimePool(workspace_fixture)
    resolver = WorkspaceResolver(pool, settings=Settings(environment={}))
    with pytest.raises(PermissionError if target != "instance" else ContextRuntimeUnavailable):
        resolver.resolve("tenant", "workspace")
    assert not pool._runtimes
    resolver.close()


def test_bad_secret_documents_do_not_expose_contents(registry):
    environment = {
        "QWENPAW_DATA_GRAPH_ENDPOINT": "bolt://localhost:7687",
        "QWENPAW_DATA_GRAPH_SECRET": '{"password":"test-password", invalid}',
    }
    resolver = WorkspaceResolver(WorkspaceRuntimePool(workspace_fixture), settings=Settings(environment=environment))
    with pytest.raises(ContextRuntimeUnavailable) as error:
        resolver.resolve("tenant", "workspace")
    assert "test-password" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    resolver.close()


def test_loader_dependency_is_optional_and_forwarded_to_real_ingest_adapter_call():
    constructor = ast.parse(textwrap.dedent(inspect.getsource(ContextRuntime.__init__)))
    calls = [node for node in ast.walk(constructor) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "IngestAdapter"]
    assert len(calls) == 1
    loaders = [keyword.value for keyword in calls[0].keywords if keyword.arg == "loader"]
    assert len(loaders) == 1 and isinstance(loaders[0], ast.Name) and loaders[0].id == "history_loader"
    for factory in (ContextRuntime, create_context_runtime, create_service_runtime):
        assert inspect.signature(factory).parameters["history_loader"].default is None


def test_remote_history_mode_cannot_fall_back_to_sql_loader():
    with pytest.raises(ValueError, match="explicit history_loader"):
        create_service_runtime(history_source="remote")
    with pytest.raises(ValueError, match="sql or remote"):
        create_service_runtime(history_source="disabled")


def test_service_initializer_forwards_explicit_history_mode_and_loader(monkeypatch):
    calls = []
    sentinel = object()

    class DependencyDatabase:
        def validate_dependencies(self, *, history_source):
            calls.append(("validate", history_source))

    database = DependencyDatabase()
    import context_manager.runtime.startup as startup

    monkeypatch.setattr(startup, "initialize_database", lambda: database)

    def capture_factory(route, **kwargs):
        assert kwargs["history_loader"] is sentinel
        return kwargs["initializer"]

    monkeypatch.setattr(startup, "create_context_runtime", capture_factory)

    async def initialize_owner_tables():
        calls.append(("initialize", "owner"))

    initializer = startup.create_service_runtime(
        history_source="remote", history_loader=sentinel, semantic_initializer=initialize_owner_tables
    )
    assert asyncio.run(initializer()) is database
    assert calls == [("initialize", "owner"), ("validate", "remote")]


def test_execution_resources_cancel_safe_close_drains_all_three_real_budgets():
    async def scenario():
        settings = ExecutionSettings(**{
            name: PoolSettings(workers=1, max_waiters=1) for name in ("graph_read", "background", "sql")
        })
        execution = ExecutionResources(settings)
        entered = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event()

        async def operation(index):
            entered[index].set()
            await release.wait()
            return index

        pools = [execution.graph_read, execution.background, execution.sql]
        work = [asyncio.create_task(pool.run(operation, index)) for index, pool in enumerate(pools)]
        await asyncio.gather(*(event.wait() for event in entered))
        closing = asyncio.Event()

        async def close():
            closing.set()
            await execution.aclose()

        shutdown = asyncio.create_task(close())
        await closing.wait()
        shutdown.cancel()
        release.set()
        assert await asyncio.gather(*work) == [0, 1, 2]
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        for pool in pools:
            assert pool._admission._active == 0
            with pytest.raises(BlockingIOError, match="closed"):
                await pool.run(lambda: None)
        await execution.aclose()

    asyncio.run(scenario())


def test_context_shutdown_is_cancel_safe_idempotent_and_dependency_ordered(monkeypatch):
    async def scenario():
        events = []
        runtime = runtime_fixture(None)
        cron_entered, release = asyncio.Event(), asyncio.Event()

        async def step(name):
            events.append(name)

        async def cron_shutdown():
            cron_entered.set()
            await release.wait()
            events.append("cron")

        async def sweep():
            try:
                await asyncio.Event().wait()
            finally:
                events.append("sweep")

        sweep_started = asyncio.Event()

        async def started_sweep():
            sweep_started.set()
            await sweep()

        runtime._sweep_task = asyncio.create_task(started_sweep())
        await sweep_started.wait()
        runtime.cron = SimpleNamespace(shutdown=cron_shutdown)
        runtime.embedding_jobs = SimpleNamespace(aclose=lambda: step("embedding"))
        runtime._recovery = SimpleNamespace(aclose=lambda: step("recovery"))
        runtime._resolver = SimpleNamespace(stop_services=lambda: step("workspace-services"), close=lambda: events.append("stores"))
        runtime._execution = SimpleNamespace(aclose=lambda: step("executors"))
        runtime._usage_recorder = SimpleNamespace(stop=lambda: events.append("usage"))
        runtime._database = object()
        monkeypatch.setattr(context_module, "close_database", lambda: events.append("database"))
        first = asyncio.create_task(runtime.aclose())
        await cron_entered.wait()
        second = asyncio.create_task(runtime.aclose())
        first.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await runtime.aclose()
        assert runtime.closed
        assert events == ["sweep", "cron", "embedding", "recovery", "workspace-services", "executors", "stores", "usage", "database"]

    asyncio.run(scenario())


def test_shutdown_runs_remaining_cleanup_even_when_service_stop_fails(monkeypatch):
    async def scenario():
        events = []
        runtime = runtime_fixture(None)

        async def fail():
            raise RuntimeError("cron stop failed")

        async def stop(name):
            events.append(name)

        runtime.cron = SimpleNamespace(shutdown=fail)
        runtime.embedding_jobs = SimpleNamespace(aclose=lambda: stop("embedding"))
        runtime._resolver = SimpleNamespace(stop_services=lambda: stop("services"), close=lambda: events.append("stores"))
        runtime._execution = SimpleNamespace(aclose=lambda: stop("executors"))
        runtime._database = object()
        monkeypatch.setattr(context_module, "close_database", lambda: events.append("database"))
        with pytest.raises(RuntimeError, match="cron stop failed"):
            await runtime.aclose()
        assert runtime.closed
        assert events == ["embedding", "services", "executors", "stores", "database"]

    asyncio.run(scenario())
