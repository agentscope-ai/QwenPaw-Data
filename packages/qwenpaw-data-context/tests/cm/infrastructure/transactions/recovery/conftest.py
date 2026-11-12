"""Use real SQLite receipts and the existing disposable local Neo4j fixture."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from context_manager.auth import GLOBAL_OWNER_ID
from context_manager.executor import BoundedExecutor
from context_manager.schema.relational import recovery_runs
from context_manager.settings.execution import PoolSettings
from context_manager.transaction.recovery import RecoveryService
from context_manager.transaction.recovery.repository import RecoveryRunRepository
from context_manager.transaction.recovery.stack import RecoveryStack
from sqlalchemy import create_engine

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)


@pytest.fixture
def runs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    recovery_runs.create(engine)
    yield RecoveryRunRepository(engine)
    engine.dispose()


@pytest.fixture
async def recovery(graph, runs):  # noqa: F811 - imported pytest fixture
    store, _, (scope, other_scope) = graph
    executor = BoundedExecutor("recovery-test", PoolSettings(workers=2))
    system = authorization(
        user_id="__system__", workspace_id=scope.workspace_id, role="ws_admin"
    )
    control = store.access.issue_admin_scope(
        system, owner_id=GLOBAL_OWNER_ID, zone="_internal"
    )
    read = store.access.issue_admin_scope(system)

    def stack_for(identity):
        # Stand in only for Runtime resource/identity assembly, never graph I/O.
        assert (identity["tenant_id"], identity["workspace_id"]) == (
            scope.tenant_id,
            scope.workspace_id,
        )
        return RecoveryStack(
            store,
            identity["rid"],
            control_scope=control,
            read_scope=read,
            scope_for=lambda owner, datasource: store.access.issue_admin_scope(
                system, owner_id=owner, datasource_id=datasource
            ),
        )

    service = RecoveryService(runs, executor, stack_for)
    identity = {
        "tenant_id": scope.tenant_id,
        "workspace_id": scope.workspace_id,
        "task_type": "test",
        "task_id": "task-1",
        "rid": uuid4().hex,
    }
    yield SimpleNamespace(
        store=store,
        scope=scope,
        other_scope=other_scope,
        control=control,
        executor=executor,
        runs=runs,
        stack_for=stack_for,
        service=service,
        identity=identity,
    )
    await service.aclose()
    await executor.aclose()
