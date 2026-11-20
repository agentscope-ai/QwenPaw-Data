"""Coordinate history ingestion through one claim row per user."""

import time

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from context_manager.heartbeat import stale_cutoff
from context_manager.utils.pod import current_pod_name

from ..db import get_database
from ..schema.relational import user_ingest_claims as claims
from .loader import UserHistoryScope


class UserIngestRepository:
    """Claim, refresh and release one user's ingestion round."""

    @staticmethod
    def _owner(scope: UserHistoryScope) -> dict[str, str]:
        return dict(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
        )

    def claim(self, scope: UserHistoryScope) -> str | None:
        now = time.time()
        cutoff = stale_cutoff(now)
        engine = get_database().engine
        insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
        statement = (
            insert(claims)
            .values(
                **self._owner(scope),
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
        with engine.begin() as connection:
            epoch = connection.execute(statement).scalar_one_or_none()
        return None if epoch is None else str(epoch)

    def renew(self, scope: UserHistoryScope, token: str) -> bool:
        engine = get_database().engine
        with engine.begin() as connection:
            result = connection.execute(
                update(claims)
                .where(
                    *(
                        claims.c[name] == value
                        for name, value in self._owner(scope).items()
                    ),
                    claims.c.run_epoch == int(token),
                    claims.c.heartbeat_at.is_not(None),
                )
                .values(heartbeat_at=time.time())
            )
        return result.rowcount == 1

    def release(self, scope: UserHistoryScope, token: str) -> None:
        engine = get_database().engine
        with engine.begin() as connection:
            connection.execute(
                update(claims)
                .where(
                    *(
                        claims.c[name] == value
                        for name, value in self._owner(scope).items()
                    ),
                    claims.c.run_epoch == int(token),
                )
                .values(heartbeat_at=None, executor_pod=None)
            )
