# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

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
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.repositories.snapshot import SnapshotRepository
from qwenpaw_data.host.core.stream.output_stream import OutputStream

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


async def test_biz_event_upsert_and_segment_coverage_snapshot(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        async with factory() as db:
            session = Session.create(identity=TEST_IDENTITY, title="segment")
            session_repo = SessionRepository(db, TEST_IDENTITY)
            await session_repo.add(session)
            chat = session.open_chat(
                text="分析 DAU",
                datasource_id="ds_demo",
                has_active_chat=False,
            )
            await session_repo.save(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
        stream = OutputStream(
            factory,
            session_id=session.id,
            chat_id=chat.id,
            identity=TEST_IDENTITY,
            last_sequence_number=chat.last_sequence_number,
        )
        first = await stream.biz_event(
            event_id="event-1",
            seq=10,
            channel="main",
            block_id="call-1",
            status="done",
            presentation={
                "card_type": "tool",
                "caption": "执行 SQL",
                "body": "执行中",
            },
            started_at=1.0,
            ended_at=None,
        )
        updated = await stream.biz_event(
            event_id="event-1",
            seq=99,
            channel="main",
            block_id="call-1",
            status="error",
            presentation={
                "card_type": "tool",
                "caption": "执行 SQL",
                "body": "已完成",
            },
            started_at=1.0,
            ended_at=2.0,
        )
        await stream.segment(
            segment_id="segment-1",
            title="定位变化点",
            input=None,
            behavior="读取并比较时间序列",
            conclusion="变化发生在月中",
            artifact=[
                {
                    "name": "result.csv",
                    "description": "变化点结果",
                    "relative_path": "analysis/result.csv",
                }
            ],
            coverage={"start_seq": 10, "end_seq": 10},
            started_at=1.0,
            ended_at=2.0,
        )
        async with factory() as db:
            bundle = await SnapshotRepository(db, TEST_IDENTITY).bundle_chat(chat)
            assert first.biz_event.seq == 10
            assert updated.biz_event.seq == 10
            assert bundle.biz_events[0]["status"] == "error"
            assert bundle.biz_events[0]["block_id"] == "call-1"
            assert bundle.segments[0]["coverage"] == {
                "start_seq": 10,
                "end_seq": 10,
            }
            assert bundle.segments[0]["artifact"][0]["relative_path"] == (
                "analysis/result.csv"
            )
    finally:
        await reset_engine()


async def test_same_algorithm_ids_across_chats_stay_separate(tmp_path: Path) -> None:
    """算法侧 id 只在 chat 内唯一：两轮 chat 各自的 seg_0001 / event-1 互不干扰。"""
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        async with factory() as db:
            session = Session.create(identity=TEST_IDENTITY, title="cross")
            session_repo = SessionRepository(db, TEST_IDENTITY)
            await session_repo.add(session)
            chat_repo = ChatRepository(db, TEST_IDENTITY)

            chats = []
            for text in ("第一轮", "第二轮"):
                chat = session.open_chat(
                    text=text, datasource_id="ds_demo", has_active_chat=False
                )
                chat.mark_status("completed")
                await session_repo.save(session)
                await chat_repo.add(chat)
                chats.append(chat)
            await db.commit()

        for i, chat in enumerate(chats):
            stream = OutputStream(
                factory,
                session_id=session.id,
                chat_id=chat.id,
                identity=TEST_IDENTITY,
                last_sequence_number=chat.last_sequence_number,
            )
            await stream.biz_event(
                event_id="event-1",
                seq=1,
                block_id=f"call-{i}",
                status="done",
                started_at=1.0,
            )
            await stream.segment(
                segment_id="seg_0001",
                title=f"段落-{i}",
                coverage={"start_seq": 1, "end_seq": 1},
            )
        async with factory() as db:
            for i, chat in enumerate(chats):
                bundle = await SnapshotRepository(db, TEST_IDENTITY).bundle_chat(chat)
                assert [e["event_id"] for e in bundle.biz_events] == ["event-1"]
                assert bundle.biz_events[0]["block_id"] == f"call-{i}"
                assert [s["segment_id"] for s in bundle.segments] == ["seg_0001"]
                assert bundle.segments[0]["title"] == f"段落-{i}"
    finally:
        await reset_engine()
