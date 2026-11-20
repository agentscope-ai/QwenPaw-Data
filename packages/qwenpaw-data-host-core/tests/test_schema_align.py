# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from qwenpaw_data.host.core.db.align import SchemaAlignError
from qwenpaw_data.host.core.db.engine import get_engine, init_db, reset_engine
from qwenpaw_data.host.core.db.tables import Base


async def _pragma_columns(table: str) -> set[str]:
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    return {row[1] for row in rows}


async def _index_names(table: str) -> set[str]:
    engine = get_engine()
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda c: {idx["name"] for idx in inspect(c).get_indexes(table)}
        )


@pytest.mark.asyncio
async def test_init_db_creates_host_tables_and_is_idempotent() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert set(Base.metadata.tables) <= names
    assert "alembic_version" not in names
    assert "ix_skill_proposals_session_status" in await _index_names("skill_proposals")
    await init_db()


@pytest.mark.asyncio
async def test_init_db_adds_missing_nullable_column() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE sessions DROP COLUMN deleted_at"))
    assert "deleted_at" not in await _pragma_columns("sessions")

    await init_db()
    assert "deleted_at" in await _pragma_columns("sessions")


@pytest.mark.asyncio
async def test_init_db_rejects_not_null_column_without_server_default() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE sessions DROP COLUMN title"))

    with pytest.raises(SchemaAlignError, match="sessions.title"):
        await init_db()


@pytest.mark.asyncio
async def test_init_db_ignores_nullable_mismatch_on_existing_column() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE chats DROP COLUMN artifact_comments_json"))
        await conn.execute(
            text("ALTER TABLE chats ADD COLUMN artifact_comments_json JSON")
        )
    await init_db()
    engine = get_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("PRAGMA table_info(chats)"))).all()
    cols = {r[1]: r[3] for r in row}  # name -> notnull
    assert "artifact_comments_json" in cols
    assert cols["artifact_comments_json"] == 0


@pytest.mark.asyncio
async def test_init_db_adds_missing_index() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX ix_skill_proposals_session_status"))
    assert "ix_skill_proposals_session_status" not in await _index_names(
        "skill_proposals"
    )

    await init_db()
    assert "ix_skill_proposals_session_status" in await _index_names("skill_proposals")


@pytest.mark.asyncio
async def test_init_db_leaves_foreign_tables_and_leftover_columns() -> None:
    await reset_engine()
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE tenants (id TEXT PRIMARY KEY)"))
        await conn.execute(text("ALTER TABLE sessions ADD COLUMN leftover TEXT"))

    await init_db()
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert "tenants" in names
    assert "leftover" in await _pragma_columns("sessions")
