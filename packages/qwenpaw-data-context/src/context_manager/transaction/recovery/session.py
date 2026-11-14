"""Coordinate one execution's SQL receipt, graph stack and asynchronous exit."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from typing import Self

from ...auth import ScopeBinding
from ...executor import BoundedExecutor, drain
from ...heartbeat import Heartbeat
from ...store import StoreError
from ...utils.pod import current_pod_name
from ..plan import TransactionPlan, TransactionResult
from .repository import RecoveryRunRepository
from .stack import RecoveryStack
from .stacked import StackedTransaction

log = logging.getLogger(__name__)


class RecoveryStackSession:
    """Execute sequential batches; RecoveryService owns this session's admission.

    The synchronous execute method fits existing executor.run_sync call sites.
    Closing waits for its in-flight batch before deciding whether to undo it.
    Business task writers consume the durable receipt by task key and rid.
    """

    def __init__(
        self,
        stack: RecoveryStack,
        runs: RecoveryRunRepository,
        executor: BoundedExecutor,
        identity: Mapping[str, str],
        scope: ScopeBinding | None,
        *,
        previous_rid: str | None = None,
    ) -> None:
        self._stack = stack
        self._runs = runs
        self._executor = executor
        self._identity = identity
        self._scope = scope
        self._previous_rid = previous_rid
        self.rid = identity["rid"]
        self.receipt: Mapping[str, object] | None = None
        self._opened = False
        self._failed = False
        self._sequence = 0
        self._batch_lock = Lock()
        self._result = ("SUCCESS", None)
        self._heartbeat: Heartbeat | None = None

    async def _beat(self) -> None:
        await self._executor.run(self._runs.heartbeat, self._identity)

    def _start_heartbeat(self) -> None:
        if self._heartbeat is not None:
            return
        self._heartbeat = Heartbeat(self._beat)
        self._heartbeat.start()

    async def _stop_heartbeat(self) -> None:
        heartbeat = self._heartbeat
        self._heartbeat = None
        if heartbeat is not None:
            await heartbeat.stop()

    async def __aenter__(self) -> Self:
        await self._executor.run(
            self._runs.register,
            self._identity,
            previous_rid=self._previous_rid,
            live=True,
        )
        self._start_heartbeat()
        try:
            await self._executor.run(self._stack.create)
            await self._owned_update(
                values={"recovery_state": "ACTIVE"},
                states=("REGISTERED",),
            )
        except BaseException:
            await self._stop_heartbeat()
            await self._executor.run(self._runs.release_if_open, self._identity)
            raise
        self._opened = True
        return self

    async def __aexit__(self, exc_type, error, traceback) -> bool:
        self._opened = False
        try:
            # Keep refreshing through undo and cleanup. Stopping first lets
            # another process claim the run while this one is still writing.
            await drain(asyncio.create_task(self._close(error)))
        finally:
            await self._stop_heartbeat()
            await self._executor.run(self._runs.release_if_open, self._identity)
        return False

    async def _owned_update(
        self, *, values: Mapping[str, object], states: tuple[str, ...]
    ) -> None:
        await self._executor.run(
            self._runs.update,
            self._identity,
            values=values,
            states=states,
            owner=current_pod_name(),
        )

    def execute(
        self,
        plan: TransactionPlan,
        scope: ScopeBinding | Sequence[ScopeBinding],
        *,
        validate: Callable[[TransactionResult], None] | None = None,
    ) -> TransactionResult:
        if not self._batch_lock.acquire(blocking=False):
            self._failed = True
            raise RuntimeError("recovery batches must execute sequentially")
        try:
            if not self._opened or self._failed:
                raise RuntimeError("recovery session no longer accepts batches")
            result = StackedTransaction(
                self._stack, self._scope, self._sequence + 1
            ).execute(plan, scope, validate=validate)
            self._sequence += 1
            return result
        except BaseException:
            # Even a caught error or unknown commit must prevent later success.
            self._failed = True
            raise
        finally:
            self._batch_lock.release()

    def set_result(self, status: str, message: str | None = None) -> None:
        """Annotate a business outcome; ordinary success needs no explicit call."""
        if status not in {"SUCCESS", "DEGRADED", "FAILED", "KILLED"}:
            raise ValueError("unsupported recovery result status")
        self._result = status, message[:2048] if message else None

    def _decide(self, error: BaseException | None):
        with self._batch_lock:
            failure = error
            if failure is None and self._failed:
                failure = RuntimeError("a failed batch prevents committing this run")
            status, message = self._result
            if failure is not None:
                status = (
                    "KILLED"
                    if isinstance(failure, asyncio.CancelledError)
                    else "FAILED"
                )
                message = str(failure)[:2048] or None
            return self._stack.decide(status, message), failure

    async def _close(self, error: BaseException | None) -> None:
        try:
            await self._beat()
        except StoreError:
            log.debug("recovery close skipped a run this process no longer owns")
            return
        head, failure = await self._executor.run(self._decide, error)
        await self._settle({**self._identity, "recovery_state": "ACTIVE"}, head)
        if error is None and failure is not None:
            raise failure

    async def recover(self) -> None:
        """Resume a persisted decision or undo abandoned writes, never replay them."""
        claimed = await self._executor.run(self._runs.claim_abandoned, self._identity)
        if not claimed:
            log.debug("recovery run %s still has a live owner", self.rid)
            return
        self._start_heartbeat()
        try:
            await self._recover()
        finally:
            await self._stop_heartbeat()
            await self._executor.run(self._runs.release_if_open, self._identity)

    async def _recover(self) -> None:
        row = await self._executor.run(self._runs.get, self._identity)
        if row["rid"] != self.rid:
            raise StoreError(
                "recovery execution was superseded",
                operation="recovery",
                code="recovery_fenced",
            )
        if row["recovery_state"] == "CLOSED":
            self.receipt = row
            return
        head = await self._executor.run(self._stack.head)
        if head is None and row["recovery_state"] == "REGISTERED":
            await self._executor.run(
                self._stack.create, state="ROLLED_BACK", result_status="FAILED"
            )
            head = await self._executor.run(self._stack.head)
        if head is not None and head.properties["recovery_state"] == "ACTIVE":
            values = await self._executor.run(
                self._stack.decide, "FAILED", "execution was interrupted"
            )
        else:
            values = head.properties if head is not None else None
        await self._settle(row, values)

    async def _settle(self, row, head) -> None:
        if row["recovery_state"] != "CLEANING":
            if head is None:
                raise StoreError(
                    "an open recovery run lost its head",
                    operation="recovery",
                    code="recovery_head_missing",
                )
            if head["recovery_state"] == "RECOVERING":
                await self._owned_update(
                    values={"recovery_state": "RECOVERING"},
                    states=("REGISTERED", "ACTIVE", "RECOVERING"),
                )
                for sequence in range(head["head_seq"], 0, -1):
                    await self._beat()
                    await self._executor.run(self._stack.undo_batch, sequence)
                head = (await self._executor.run(self._stack.head)).properties
            if head["recovery_state"] not in {"COMMITTED", "ROLLED_BACK"}:
                raise RuntimeError("recovery result is not terminal")
            result = {
                "recovery_state": "CLEANING",
                "result_status": head["result_status"],
                "message": head.get("message"),
            }
            await self._owned_update(
                values=result,
                states=("REGISTERED", "ACTIVE", "RECOVERING"),
            )
            row = {**row, **result}
        while True:
            await self._beat()
            if await self._executor.run(self._stack.cleanup_batch):
                break
        await self._owned_update(
            values={"recovery_state": "CLOSED"},
            states=("CLEANING",),
        )
        self.receipt = {**row, "recovery_state": "CLOSED"}
