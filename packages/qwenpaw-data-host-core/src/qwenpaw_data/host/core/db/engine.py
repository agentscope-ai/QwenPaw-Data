# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from qwenpaw_data_auth import DEFAULT_PRINCIPAL
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qwenpaw_data.host.core.db.align import align_host_schema
from qwenpaw_data.host.core.settings import get_settings

_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None

TEST_USER_ID = DEFAULT_PRINCIPAL.user_id
TEST_TENANT_ID = DEFAULT_PRINCIPAL.tenant_id
TEST_WORKSPACE_ID = DEFAULT_PRINCIPAL.active_workspace_id


def get_engine() -> AsyncEngine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    db = get_settings().db
    url = db.url
    sa_url = make_url(url)
    backend = sa_url.get_backend_name()
    engine_kwargs: dict = {
        "pool_size": db.pool_size,
        "max_overflow": db.max_overflow,
    }
    if backend == "sqlite":
        database = sa_url.database
        if not database:
            raise ValueError(f"sqlite QWENPAW_DATA_DB_URL missing database path: {url}")
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    elif backend == "postgresql":
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = db.pool_recycle_seconds
    else:
        raise ValueError(
            f"QWENPAW_DATA_DB_URL backend must be sqlite or postgresql, got {backend}"
        )

    engine = create_async_engine(url, **engine_kwargs)

    if backend == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _engine = engine
    _SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def init_db() -> AsyncEngine:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(align_host_schema)
    return engine


async def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _SessionLocal = None
