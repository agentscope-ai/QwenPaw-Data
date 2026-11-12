# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_engine,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import ChatEventRow, ChatRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.time import utcnow


def _response_event(session_id: str, chat_id: str, status: str) -> ChatEventRow:
    return ChatEventRow(
        chat_id=chat_id,
        sequence_number=0,
        object="response",
        session_id=session_id,
        payload={"object": "response", "id": "resp_1", "status": status},
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )


TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


async def _open_running_chat(db, title: str) -> tuple[str, str]:
    session = Session.create(identity=TEST_IDENTITY, agent_id="default", title=title)
    repo = SessionRepository(db, TEST_IDENTITY)
    await repo.add(session)
    chat = session.open_chat(text=title, datasource_id="ds-1", has_active_chat=False)
    await repo.save(session)
    await ChatRepository(db, TEST_IDENTITY).add(chat)
    await db.commit()
    return session.id, chat.id


@pytest.mark.asyncio
async def test_align_leaves_mid_flight_running(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    try:
        async with factory() as db:
            _, chat_id = await _open_running_chat(db, "orphan")
        async with factory() as db:
            assert await ChatRepository.recover_running(db) == 0
            await db.commit()
        async with factory() as db:
            assert (
                await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            ).status == "running"
    finally:
        reset_hub()
        await reset_engine()


@pytest.mark.asyncio
async def test_align_copies_terminal_event_status(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    try:
        async with factory() as db:
            session_id, chat_id = await _open_running_chat(db, "split")
        async with factory() as db:
            await OutputStream(
                factory,
                session_id=session_id,
                chat_id=chat_id,
                identity=TEST_IDENTITY,
                last_sequence_number=-1,
            ).response_cancelled()
        async with factory() as db:
            assert await ChatRepository.recover_running(db) == 1
            await db.commit()
        async with factory() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            assert chat.status == "canceled"
            assert not await SessionRepository(db, TEST_IDENTITY).has_active_chat(
                session_id
            )
    finally:
        reset_hub()
        await reset_engine()


@pytest.mark.asyncio
async def test_align_copies_canceled_event_status(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    try:
        async with factory() as db:
            session_id, chat_id = await _open_running_chat(db, "canceled-spelling")
            db.add(_response_event(session_id, chat_id, "canceled"))
            await db.commit()
        async with factory() as db:
            assert await ChatRepository.recover_running(db) == 1
            await db.commit()
        async with factory() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            assert chat.status == "canceled"
            assert not await SessionRepository(db, TEST_IDENTITY).has_active_chat(
                session_id
            )
    finally:
        reset_hub()
        await reset_engine()


@pytest.mark.asyncio
async def test_recover_raises_on_unknown_response_status(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    try:
        async with factory() as db:
            session_id, chat_id = await _open_running_chat(db, "unknown-status")
            db.add(_response_event(session_id, chat_id, "bogus"))
            await db.commit()
        async with factory() as db:
            with pytest.raises(ValueError, match="unknown response status"):
                await ChatRepository.recover_running(db)
    finally:
        reset_hub()
        await reset_engine()


@pytest.mark.asyncio
async def test_recover_cancels_stale_heartbeat(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    try:
        async with factory() as db:
            session_id, chat_id = await _open_running_chat(db, "stale")
        async with factory() as db:
            row = await db.get(ChatRow, chat_id)
            row.heartbeat_at = utcnow() - timedelta(seconds=60)
            await db.commit()
        async with factory() as db:
            assert await ChatRepository.recover_running(db) == 1
            await db.commit()
        async with factory() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            assert chat.status == "canceled"
            events = (
                await db.scalars(
                    select(ChatEventRow).where(
                        ChatEventRow.chat_id == chat_id,
                        ChatEventRow.object == "response",
                    )
                )
            ).all()
            assert [(e.payload or {}).get("status") for e in events] == ["cancelled"]
            assert not await SessionRepository(db, TEST_IDENTITY).has_active_chat(
                session_id
            )
    finally:
        reset_hub()
        await reset_engine()


@pytest.mark.asyncio
async def test_sqlite_engine_enables_wal_and_busy_timeout() -> None:
    await reset_engine()
    await init_db()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        assert str(journal).lower() == "wal"
        assert int(timeout) >= 5000
    finally:
        await reset_engine()
