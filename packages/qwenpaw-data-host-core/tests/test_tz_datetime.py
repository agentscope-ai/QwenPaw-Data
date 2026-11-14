# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.db.types import TZDateTime
from qwenpaw_data.host.core.utils.time import utcnow


def test_mapped_datetime_uses_tz_datetime() -> None:
    assert isinstance(SessionRow.__table__.c.created_at.type, TZDateTime)
    assert isinstance(ChatRow.__table__.c.started_at.type, TZDateTime)
    assert isinstance(ChatRow.__table__.c.completed_at.type, TZDateTime)


def test_bind_rejects_naive() -> None:
    with pytest.raises(TypeError, match="tzinfo is required"):
        TZDateTime().process_bind_param(datetime(2026, 1, 1), sqlite.dialect())


def test_result_attaches_utc_to_naive() -> None:
    value = TZDateTime().process_result_value(
        datetime(2026, 1, 1, 12, 0, 0), sqlite.dialect()
    )
    assert value.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_sqlite_roundtrip_restores_utc_and_allows_subtraction() -> None:
    await reset_engine()
    await init_db()
    try:
        started = utcnow()
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                SessionRow(
                    id="ses_tz",
                    user_id=TEST_USER_ID,
                    tenant_id=TEST_TENANT_ID,
                    workspace_id=TEST_WORKSPACE_ID,
                    created_at=started,
                    updated_at=started,
                )
            )
            db.add(
                ChatRow(
                    id="chat_tz",
                    session_id="ses_tz",
                    user_id=TEST_USER_ID,
                    tenant_id=TEST_TENANT_ID,
                    workspace_id=TEST_WORKSPACE_ID,
                    sequence=1,
                    started_at=started,
                )
            )
            await db.commit()

        async with factory() as db:
            session = await db.get(SessionRow, "ses_tz")
            chat = await db.get(ChatRow, "chat_tz")
            assert session.created_at.tzinfo is timezone.utc
            assert chat.started_at.tzinfo is timezone.utc
            elapsed = utcnow() - chat.started_at
            assert elapsed >= timedelta(0)
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_sqlite_rejects_naive_on_write() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                SessionRow(
                    id="ses_naive",
                    user_id=TEST_USER_ID,
                    tenant_id=TEST_TENANT_ID,
                    workspace_id=TEST_WORKSPACE_ID,
                    created_at=datetime(2026, 1, 1),
                    updated_at=utcnow(),
                )
            )
            with pytest.raises(StatementError, match="tzinfo is required"):
                await db.flush()
    finally:
        await reset_engine()
