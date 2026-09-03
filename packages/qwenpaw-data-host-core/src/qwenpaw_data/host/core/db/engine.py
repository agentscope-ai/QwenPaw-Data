# -*- coding: utf-8 -*-
"""Async engine helpers for the host service database.

No module-global singleton: the app owns the engine lifecycle (created in
the lifespan, disposed on shutdown), stores hold the session factory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from qwenpaw_data.host.core.db.tables import Base

DB_URL_ENV = "QWENPAW_DATA_DB_URL"


def resolve_db_url(
    home: Path,
    env: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if env is None else env
    url = str(values.get(DB_URL_ENV, "")).strip()
    if url:
        return url
    return f"sqlite+aiosqlite:///{home / 'host' / 'host.db'}"


def create_engine_and_factory(
    url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    sa_url = make_url(url)
    if sa_url.get_backend_name() == "sqlite":
        database = sa_url.database
        if not database:
            raise ValueError(f"sqlite {DB_URL_ENV} missing database path: {url}")
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url)

    if sa_url.get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            await conn.execute(CreateTable(table, if_not_exists=True))
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn: Connection) -> None:
    """Additive migration: columns introduced after a table already exists.

    Added as nullable regardless of the model (SQLite cannot ADD COLUMN
    NOT NULL without a default); readers treat NULL as the empty value.
    """
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            column_type = column.type.compile(conn.dialect)
            conn.execute(
                text(
                    f'ALTER TABLE "{table.name}" '
                    f'ADD COLUMN "{column.name}" {column_type}'
                )
            )
