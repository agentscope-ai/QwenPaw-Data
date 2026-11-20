"""Persist processed message versions in the shared control-plane database."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy.engine import Engine
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ...db import get_database
from ...schema.relational import update_checkpoints as _checkpoints
from .contract import ChatHistoryMessage, UserHistoryScope


class ChatLoaderCheckpointRepository:
    """Record each successfully processed message and extraction version."""

    @property
    def _engine(self) -> Engine:
        """Use the service pool for history checkpoints."""
        return get_database().engine

    def snapshot(
        self,
        scope: UserHistoryScope,
        *,
        updater_version: str,
    ) -> Mapping[str, str]:
        """Read one User's source versions processed by this extraction version."""
        statement = select(
            _checkpoints.c.source_id,
            _checkpoints.c.source_version,
        ).where(
            _checkpoints.c.graph_name == _graph_name(scope),
            _checkpoints.c.updater_version == updater_version,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return {row["source_id"]: row["source_version"] for row in rows}

    def mark_processed(
        self,
        scope: UserHistoryScope,
        message: ChatHistoryMessage,
        *,
        updater_version: str,
        processed_at: datetime,
    ) -> None:
        """Upsert the exact input version only after its graph commit succeeds."""
        timestamp = _iso_timestamp(processed_at)
        engine = self._engine
        insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
        statement = insert(_checkpoints).values(
            graph_name=_graph_name(scope),
            source_id=message.source_id,
            source_version=message.source_version,
            updater_version=updater_version,
            processed_at=timestamp,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[_checkpoints.c.graph_name, _checkpoints.c.source_id],
            set_={
                name: statement.excluded[name]
                for name in ("source_version", "updater_version", "processed_at")
            },
        )
        with engine.begin() as connection:
            connection.execute(statement)

    def is_processed(
        self,
        scope: UserHistoryScope,
        message: ChatHistoryMessage,
        *,
        updater_version: str,
    ) -> bool:
        """Recheck one revision immediately before processing it."""
        statement = select(_checkpoints.c.source_id).where(
            _checkpoints.c.graph_name == _graph_name(scope),
            _checkpoints.c.source_id == message.source_id,
            _checkpoints.c.source_version == message.source_version,
            _checkpoints.c.updater_version == updater_version,
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).first() is not None


def _graph_name(scope: UserHistoryScope) -> str:
    """Match develop's scoped User Graph checkpoint namespace."""
    return "user:" + ":".join(
        quote(part, safe="")
        for part in (scope.tenant_id, scope.workspace_id, scope.user_id)
    )


def _iso_timestamp(value: datetime) -> str:
    """Serialize one aware timestamp in stable UTC form."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be a timezone-aware datetime")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


__all__ = ["ChatLoaderCheckpointRepository"]
