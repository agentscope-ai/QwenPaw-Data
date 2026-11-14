"""Serialize one check-then-insert onto a business key until commit.

PostgreSQL holds a transaction advisory lock. SQLite writes one row of
``cm_claim_lock`` so the next connection waits for this transaction and then
sees the inserted business row. ``cm_claim_lock`` is not a second lease.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

_SQLITE_LOCK = """
CREATE TABLE IF NOT EXISTS cm_claim_lock (
    name TEXT PRIMARY KEY
)
"""
_SQLITE_TOUCH = """
INSERT INTO cm_claim_lock (name) VALUES (:key)
ON CONFLICT(name) DO UPDATE SET name = excluded.name
"""


async def lock_scope(db: AsyncSession, key: str) -> None:
    """Block other transactions that claim the same ``key`` until commit."""
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": key},
        )
        return
    await db.execute(text(_SQLITE_LOCK))
    await db.execute(text(_SQLITE_TOUCH), {"key": key})


def lock_scope_sync(connection: Connection, key: str) -> None:
    """Same scope lock for a synchronous transaction."""
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": key},
        )
        return
    connection.execute(text(_SQLITE_LOCK))
    connection.execute(text(_SQLITE_TOUCH), {"key": key})
