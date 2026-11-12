"""Persist the current recovery attempt of each task in shared SQL."""

import time
from collections.abc import Mapping

from sqlalchemy import Engine, and_, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ...schema.relational import recovery_runs
from ...settings import get_settings
from ...store import StoreError
from ...utils.pod import current_pod_name

_TASK_KEY = ("tenant_id", "workspace_id", "task_type", "task_id")


class RecoveryRunRepository:
    """Own current-attempt registration and conditional state updates.

    A task can replace its row only after graph logs are gone and its row is CLOSED.
    A fresh heartbeat means another process still owns the run. Discovery returns
    only rows whose heartbeat is missing or older than the deployment window.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _task(self, identity: Mapping[str, object]):
        return and_(*(recovery_runs.c[name] == identity[name] for name in _TASK_KEY))

    def register(
        self,
        values: Mapping[str, object],
        *,
        previous_rid: str | None = None,
        live: bool = False,
    ) -> None:
        """Replace CLOSED only after its caller has consumed that exact receipt.

        ``live`` records this process as the owner in the same transaction.
        A registration left for a later scan keeps a null heartbeat.
        """
        current = {
            **values,
            "recovery_state": "REGISTERED",
            "result_status": None,
            "message": None,
            "heartbeat_at": time.time() if live else None,
            "executor_pod": current_pod_name() if live else None,
        }
        dialect_insert = (
            sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        )
        with self.engine.begin() as connection:
            inserted = connection.execute(
                dialect_insert(recovery_runs)
                .values(**current)
                .on_conflict_do_nothing(index_elements=list(_TASK_KEY))
                .returning(recovery_runs.c.rid)
            )
            if inserted.scalar_one_or_none() is not None:
                return
            claimed = connection.execute(
                update(recovery_runs)
                .where(
                    self._task(values),
                    recovery_runs.c.recovery_state == "CLOSED",
                    recovery_runs.c.rid == previous_rid,
                    recovery_runs.c.rid != values["rid"],
                )
                .values(**current)
            )
            if claimed.rowcount != 1:
                raise StoreError(
                    "this task still has an unresolved recovery run or reused its rid",
                    operation="recovery",
                    code="recovery_task_busy",
                )

    def get(self, identity: Mapping[str, object]) -> Mapping[str, object] | None:
        """Read the task's current attempt, including replacements of a stale rid."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(recovery_runs).where(self._task(identity)))
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def has_pending(self, tenant_id: str, workspace_id: str) -> bool:
        """Block new writes while this workspace still has recovery work.

        A REGISTERED or ACTIVE row with a fresh heartbeat is another process
        executing normally. A null or stale heartbeat, and any RECOVERING or
        CLEANING row, still owns unfinished work.
        """
        cutoff = time.time() - get_settings().heartbeat.stale_after_seconds
        pending = or_(
            recovery_runs.c.recovery_state.not_in(("REGISTERED", "ACTIVE", "CLOSED")),
            and_(
                recovery_runs.c.recovery_state.in_(("REGISTERED", "ACTIVE")),
                or_(
                    recovery_runs.c.heartbeat_at.is_(None),
                    recovery_runs.c.heartbeat_at < cutoff,
                ),
            ),
        )
        query = select(recovery_runs.c.rid).where(
            recovery_runs.c.tenant_id == tenant_id,
            recovery_runs.c.workspace_id == workspace_id,
            pending,
        )
        with self.engine.connect() as connection:
            return connection.execute(query.limit(1)).first() is not None

    def update(
        self,
        identity: Mapping[str, object],
        *,
        values: Mapping[str, object],
        states: tuple[str, ...],
        owner: str | None = None,
    ) -> None:
        """Update one attempt. ``owner`` keeps a live process from writing another's run."""
        owned = (recovery_runs.c.executor_pod == owner,) if owner is not None else ()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(recovery_runs)
                .where(
                    self._task(identity),
                    recovery_runs.c.rid == identity["rid"],
                    recovery_runs.c.recovery_state.in_(states),
                    *owned,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise StoreError(
                    "recovery registration changed",
                    operation="recovery",
                    code="recovery_fenced",
                )

    def claim_abandoned(self, identity: Mapping[str, object]) -> bool:
        """Take one run whose heartbeat is missing or already stale.

        The conditional update is the ownership fence. A second process that
        observed the same abandoned row updates zero rows and must stop.
        """
        now = time.time()
        cutoff = now - get_settings().heartbeat.stale_after_seconds
        with self.engine.begin() as connection:
            result = connection.execute(
                update(recovery_runs)
                .where(
                    self._task(identity),
                    recovery_runs.c.rid == identity["rid"],
                    recovery_runs.c.recovery_state != "CLOSED",
                    or_(
                        recovery_runs.c.heartbeat_at.is_(None),
                        recovery_runs.c.heartbeat_at < cutoff,
                    ),
                )
                .values(heartbeat_at=now, executor_pod=current_pod_name())
            )
            return result.rowcount == 1

    def heartbeat(self, identity: Mapping[str, object]) -> None:
        """Refresh a run this process already owns."""
        pod = current_pod_name()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(recovery_runs)
                .where(
                    self._task(identity),
                    recovery_runs.c.rid == identity["rid"],
                    recovery_runs.c.recovery_state != "CLOSED",
                    recovery_runs.c.executor_pod == pod,
                )
                .values(heartbeat_at=time.time(), executor_pod=pod)
            )
            if result.rowcount != 1:
                raise StoreError(
                    "recovery registration changed",
                    operation="recovery",
                    code="recovery_fenced",
                )

    def release_if_open(self, identity: Mapping[str, object]) -> None:
        """Drop this process's heartbeat so the next scan can claim the run."""
        with self.engine.begin() as connection:
            connection.execute(
                update(recovery_runs)
                .where(
                    self._task(identity),
                    recovery_runs.c.rid == identity["rid"],
                    recovery_runs.c.recovery_state != "CLOSED",
                    recovery_runs.c.executor_pod == current_pod_name(),
                )
                .values(heartbeat_at=None, executor_pod=None)
            )

    def discover(
        self, *, limit: int = 100, after: tuple[str, str, str, str] | None = None
    ) -> tuple[Mapping[str, object], ...]:
        """Page abandoned runs; retain CLOSED receipts until an explicit retry."""
        identity = tuple(recovery_runs.c[name] for name in _TASK_KEY)
        cutoff = time.time() - get_settings().heartbeat.stale_after_seconds
        query = select(recovery_runs).where(
            recovery_runs.c.recovery_state != "CLOSED",
            or_(
                recovery_runs.c.heartbeat_at.is_(None),
                recovery_runs.c.heartbeat_at < cutoff,
            ),
        )
        if after is not None:
            query = query.where(tuple_(*identity) > after)
        with self.engine.connect() as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    query.order_by(*identity).limit(limit)
                ).mappings()
            )
