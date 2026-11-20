"""Serialize one user's personal MG writes with a committed heartbeat row.

The row is the mutex. Claim, refresh and release each commit before returning,
so graph work does not hold a control-plane transaction.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from context_manager.heartbeat import stale_cutoff
from context_manager.utils.pod import current_pod_name

from ...schema.relational import mg_sync_claims as claims

logger = logging.getLogger(__name__)

# How long a second writer retries before reporting the claim busy.
# Matches the previous SQLite busy_timeout and PostgreSQL lock_timeout.
CLAIM_WAIT_SECONDS = 60.0
CLAIM_RETRY_SECONDS = 0.2


class MgSyncClaims:
    """Own claim, refresh and release for one control-plane engine."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(self, tenant_id: str, workspace_id: str, user_id: str) -> int | None:
        """Take a free or stale row. Return the new epoch, or None when it is fresh."""
        now = time.time()
        cutoff = stale_cutoff(now)
        owner = self._owner(tenant_id, workspace_id, user_id)
        insert = sqlite_insert if self._engine.dialect.name == "sqlite" else pg_insert
        statement = (
            insert(claims)
            .values(
                **owner,
                heartbeat_at=now,
                executor_pod=current_pod_name() or None,
                run_epoch=1,
            )
            .on_conflict_do_update(
                index_elements=list(claims.primary_key.columns),
                set_={
                    "heartbeat_at": now,
                    "executor_pod": current_pod_name() or None,
                    "run_epoch": claims.c.run_epoch + 1,
                },
                where=(
                    claims.c.heartbeat_at.is_(None) | (claims.c.heartbeat_at < cutoff)
                ),
            )
            .returning(claims.c.run_epoch)
        )
        with self._engine.begin() as connection:
            epoch = connection.execute(statement).scalar_one_or_none()
        return None if epoch is None else int(epoch)

    def renew(
        self, tenant_id: str, workspace_id: str, user_id: str, epoch: int
    ) -> bool:
        """Refresh this epoch. False means a newer claim now owns the row."""
        with self._engine.begin() as connection:
            result = connection.execute(
                update(claims)
                .where(
                    *self._match(tenant_id, workspace_id, user_id),
                    claims.c.run_epoch == epoch,
                    claims.c.heartbeat_at.is_not(None),
                )
                .values(heartbeat_at=time.time())
            )
        return result.rowcount == 1

    def release(
        self, tenant_id: str, workspace_id: str, user_id: str, epoch: int
    ) -> None:
        """Clear this epoch only. A lost epoch or a failed statement is logged."""
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    update(claims)
                    .where(
                        *self._match(tenant_id, workspace_id, user_id),
                        claims.c.run_epoch == epoch,
                    )
                    .values(heartbeat_at=None, executor_pod=None)
                )
        except Exception:
            logger.exception(
                "MG sync claim release failed tenant=%s workspace=%s user=%s epoch=%s",
                tenant_id,
                workspace_id,
                user_id,
                epoch,
            )
            return
        if result.rowcount != 1:
            logger.info(
                "MG sync claim release matched no row tenant=%s workspace=%s "
                "user=%s epoch=%s",
                tenant_id,
                workspace_id,
                user_id,
                epoch,
            )

    @staticmethod
    def _owner(tenant_id: str, workspace_id: str, user_id: str) -> dict[str, str]:
        return {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
        }

    @staticmethod
    def _match(tenant_id: str, workspace_id: str, user_id: str):
        return (
            claims.c.tenant_id == tenant_id,
            claims.c.workspace_id == workspace_id,
            claims.c.user_id == user_id,
        )


class ClaimRefresher:
    """Refresh one epoch until stopped or a newer claim replaces it.

    Statement errors are logged and the loop continues. Stopping interrupts the
    wait so the caller does not sit out the rest of the interval.
    """

    def __init__(self, renew: Callable[[], bool], *, interval: float) -> None:
        self._renew = renew
        self._interval = interval
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mg-sync-claim", daemon=True
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Wake the wait, then let an in-flight refresh commit before returning."""
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._renew():
                    self._lost.set()
                    return
            except Exception:
                logger.exception("MG sync claim refresh failed")
