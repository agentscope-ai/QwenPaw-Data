# -*- coding: utf-8 -*-
"""datasource."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


async def test_open_chat_without_datasource(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as _db:
            session = Session.create(identity=TEST_IDENTITY, title="no-ds")
            chat = session.open_chat(
                text="你好",
                datasource_id=None,
                has_active_chat=False,
            )
            assert chat.datasource_id is None
            assert session.datasource_id is None
            assert chat.status == "running"
            assert chat.user_input == "你好"
    finally:
        await reset_engine()


async def test_open_chat_with_datasource_binds_session(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as _db:
            session = Session.create(identity=TEST_IDENTITY, title="with-ds")
            chat = session.open_chat(
                text="查数据",
                datasource_id="mysql-abc",
                has_active_chat=False,
            )

            assert session.datasource_id == "mysql-abc"
            assert chat.datasource_id == "mysql-abc"
    finally:
        await reset_engine()


async def test_open_chat_mismatch_datasource_still_rejected(tmp_path: Path) -> None:
    """A bound session passed a different datasource_id raises mismatch."""
    await reset_engine()
    await init_db()
    try:
        session = Session.create(identity=TEST_IDENTITY, title="mismatch")
        session.open_chat(
            text="第一条",
            datasource_id="mysql-abc",
            has_active_chat=False,
        )
        assert session.datasource_id == "mysql-abc"
        with pytest.raises(ValueError, match="mismatch"):
            session.open_chat(
                text="第二条",
                datasource_id="postgres-xyz",
                has_active_chat=False,
            )
    finally:
        await reset_engine()


async def test_channel_binding_dedup(tmp_path: Path) -> None:
    """Channel inbound routing: binding points at the active session."""
    from qwenpaw_data.host.core.repositories import (
        ChannelBindingRepository,
        SessionRepository,
    )

    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            repo = SessionRepository(db, TEST_IDENTITY)
            binding = ChannelBindingRepository(db)
            session = Session.create(
                identity=TEST_IDENTITY,
                title="feishu-session",
                channel="feishu",
            )
            await repo.add(session)
            await binding.point_to(
                TEST_IDENTITY, "feishu", "feishu-chat_123", session.id
            )
            await db.commit()

            sid = await binding.get_active_session_id(
                TEST_IDENTITY, "feishu", "feishu-chat_123"
            )
            assert sid == session.id
            found = await repo.get(sid)
            assert found.channel == "feishu"

            missing = await binding.get_active_session_id(
                TEST_IDENTITY, "feishu", "feishu-other"
            )
            assert missing is None
    finally:
        await reset_engine()
