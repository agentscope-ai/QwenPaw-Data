"""Exercise full recovery flows on disposable Neo4j and real SQLite receipts.

Runtime assembly is supplied by the fixture. Failure injection changes commit
acknowledgements or SQL availability; graph writes still use real transactions.
"""

import asyncio
from threading import Event

import pytest
from context_manager.schema import (
    ENTITY,
    HIERARCHY,
    UNDO_BATCH,
)
from context_manager.store import NodeDraft, StoreError
from context_manager.transaction import StoreOperation, TransactionPlan
from context_manager.transaction.recovery import RecoveryService
from context_manager.transaction.recovery.stack import RecoveryStack
from context_manager.transaction.recovery.stacked import StackedTransaction


def create_plan(*names):
    return TransactionPlan(
        (
            StoreOperation.project_nodes(
                tuple(
                    NodeDraft(
                        ENTITY,
                        name,
                        {"identity_hash": name, "name": name, "type": "product"},
                    )
                    for name in names
                )
            ),
        )
    )


def operation(name, spec, **parameters):
    return StoreOperation(name, {"spec": spec, **parameters})


async def test_failure_undoes_all_six_writes_in_reverse_order(recovery):
    r = recovery
    a, b = r.store.create_nodes(
        ENTITY,
        (
            {"identity_hash": "a", "type": "product", "name": "original a"},
            {"identity_hash": "b", "type": "product", "name": "original b"},
        ),
        scope=r.scope,
    )
    r.store.create_edges(
        HIERARCHY, ((a, b, {"description": "original edge"}),), scope=r.scope
    )
    before_nodes = r.store.get_nodes(ENTITY, (a, b), scope=r.scope)
    before_edges = r.store.get_edges(HIERARCHY, ((a, b),), scope=r.scope)
    other = r.store.create_nodes(
        ENTITY, ({"identity_hash": "a", "type": "product"},), scope=r.other_scope
    )
    with pytest.raises(ValueError, match="business failed"):
        async with r.service.run(r.scope, task_type="test") as session:
            created = await r.executor.run(session.execute, create_plan("c"), r.scope)
            c = created.node_keys["c"]
            await r.executor.run(
                session.execute,
                TransactionPlan(
                    (
                        operation("create_edges", HIERARCHY, edges=((b, c, {}),)),
                        operation(
                            "patch_nodes",
                            ENTITY,
                            patches=(
                                (a, {"name": "changed", "description": "new field"}),
                            ),
                        ),
                        operation(
                            "patch_edges",
                            HIERARCHY,
                            patches=((a, b, {"description": "changed edge"}),),
                        ),
                    )
                ),
                r.scope,
            )
            await r.executor.run(
                session.execute,
                TransactionPlan(
                    (
                        operation("delete_edges", HIERARCHY, endpoints=((a, b),)),
                        operation("delete_nodes", ENTITY, keys=(a,)),
                        operation(
                            "patch_nodes",
                            ENTITY,
                            patches=(("ent:alice:absent", {"name": "ignored"}),),
                        ),
                    )
                ),
                r.scope,
            )
            raise ValueError("business failed")
    assert r.store.get_nodes(ENTITY, (a, b), scope=r.scope) == before_nodes
    assert r.store.get_nodes(ENTITY, (c,), scope=r.scope) == (None,)
    assert r.store.get_edges(HIERARCHY, ((a, b),), scope=r.scope) == before_edges
    assert r.store.get_nodes(ENTITY, other, scope=r.other_scope)[0] is not None
    assert session.receipt["result_status"] == "FAILED"
    assert r.store.list_nodes(UNDO_BATCH, scope=r.control) == ()


async def test_lost_forward_commit_response_does_not_repeat_business_writes(
    recovery, monkeypatch
):
    r = recovery
    original = r.store.atomic

    def retry_after_commit(work):
        original(work)
        return original(work)

    with pytest.raises(StoreError) as error:
        async with r.service.run(r.scope, task_type="test") as session:
            with monkeypatch.context() as patch:
                patch.setattr(r.store, "atomic", retry_after_commit)
                await r.executor.run(session.execute, create_plan("once"), r.scope)
    assert error.value.code == "recovery_batch_committed"
    assert r.store.list_nodes(ENTITY, scope=r.scope) == ()
    assert session.receipt["result_status"] == "FAILED"


@pytest.mark.parametrize("failed_state", ("CLEANING", "CLOSED"))
async def test_restart_keeps_committed_result_after_sql_finalization_failure(
    recovery, monkeypatch, failed_state
):
    r = recovery
    original = r.runs.update

    def unavailable(identity, *, values, states, owner=None):
        if values["recovery_state"] == failed_state:
            raise OSError("SQL unavailable")
        return original(identity, values=values, states=states, owner=owner)

    with monkeypatch.context() as patch:
        patch.setattr(r.runs, "update", unavailable)
        with pytest.raises(OSError):
            async with r.service.run(
                r.scope,
                task_type="test",
                task_id=r.identity["task_id"],
                rid=r.identity["rid"],
            ) as session:
                await r.executor.run(session.execute, create_plan("committed"), r.scope)
    before = r.runs.get(r.identity)
    assert before["recovery_state"] == (
        "ACTIVE" if failed_state == "CLEANING" else "CLEANING"
    )
    # A fresh service has no original user, business worker or active registry.
    restarted = RecoveryService(r.runs, r.executor, r.stack_for)
    try:
        await restarted.recover_pending()
    finally:
        await restarted.aclose()
    assert r.runs.get(r.identity)["recovery_state"] == "CLOSED"
    assert r.runs.get(r.identity)["result_status"] == "SUCCESS"
    assert (
        r.store.get_nodes(ENTITY, ("ent:alice:committed",), scope=r.scope)[0]
        is not None
    )
    assert r.store.list_nodes(UNDO_BATCH, scope=r.control) == ()


@pytest.mark.parametrize("lost_response", (False, True))
async def test_recovery_resumes_after_an_interrupted_inverse_transaction(
    recovery, monkeypatch, lost_response
):
    r = recovery
    r.runs.register(r.identity)
    stack = r.stack_for(r.identity)
    stack.create()
    for sequence in (1, 2):
        StackedTransaction(stack, r.scope, sequence).execute(
            create_plan(f"batch-{sequence}"), r.scope
        )
    stack.decide("FAILED", "interrupted")
    original = r.store.atomic
    pending = True

    def fail_once(work):
        nonlocal pending
        if not pending:
            return original(work)
        pending = False
        if lost_response:
            original(work)
            # The driver invokes the same inverse callback after losing its ACK.
            return original(work)

        def rollback(tx):
            work(tx)
            raise OSError("connection lost before commit")

        return original(rollback)

    with monkeypatch.context() as patch:
        patch.setattr(r.store, "atomic", fail_once)
        await r.service.recover_pending()
    if not lost_response:
        assert r.runs.get(r.identity)["recovery_state"] == "RECOVERING"
        assert stack.head().properties["head_seq"] == 2
        assert len(r.store.list_nodes(ENTITY, scope=r.scope)) == 2
        with pytest.raises(StoreError) as error:
            async with r.service.run(r.scope, task_type="test", task_id="new-task"):
                pytest.fail("new task bypassed unfinished recovery")
        assert error.value.code == "recovery_workspace_busy"
        await r.service.recover_pending()
    assert r.runs.get(r.identity)["recovery_state"] == "CLOSED"
    assert r.store.list_nodes(ENTITY, scope=r.scope) == ()


async def test_cancellation_drains_inflight_batch_before_recovery(
    recovery, monkeypatch
):
    r = recovery
    entered, release = Event(), Event()
    original = RecoveryStack.begin_batch

    def blocked(stack, tx, sequence):
        original(stack, tx, sequence)
        entered.set()
        assert release.wait(10), "test did not release the transaction"

    monkeypatch.setattr(RecoveryStack, "begin_batch", blocked)

    async def work():
        async with r.service.run(
            r.scope,
            task_type="test",
            task_id=r.identity["task_id"],
            rid=r.identity["rid"],
        ) as session:
            await r.executor.run(session.execute, create_plan("cancelled"), r.scope)

    task = asyncio.create_task(work())
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await r.service.recover_pending()
        assert r.runs.get(r.identity)["recovery_state"] == "ACTIVE"
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert r.store.list_nodes(ENTITY, scope=r.scope) == ()
    assert r.runs.get(r.identity)["recovery_state"] == "CLOSED"
    assert r.runs.get(r.identity)["result_status"] == "KILLED"
