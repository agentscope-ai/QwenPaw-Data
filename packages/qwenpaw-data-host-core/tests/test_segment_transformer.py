# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.db.engine import (
    get_session_factory,
    init_db,
    reset_engine,
    TEST_TENANT_ID,
    TEST_WORKSPACE_ID,
)
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.repositories.biz_event import BizEventRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.repositories.segment import SegmentRepository
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.utils.workspace import list_session_files
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

_TEST_IDENTITY = Identity(
    user_id="u1", tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID
)


async def _prepare_db() -> tuple[str, str]:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    async with factory() as db:
        await db.flush()
        db.add(
            SessionRow(
                id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                title="t",
            )
        )
        await db.flush()
        db.add(
            ChatRow(
                id="chat_1",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=1,
            )
        )
        await db.commit()
    return "ses_1", "chat_1"


async def _make_envelope(session_id: str, chat_id: str) -> Envelope:
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_TEST_IDENTITY,
        last_sequence_number=-1,
    )
    return Envelope(stream, session_factory=factory)


def test_send_biz_event_two_events_then_upsert(tmp_path: Path) -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        env = await _make_envelope(session_id, chat_id)

        # 一次工具调用发两条 biz_event：调用卡和结果卡。
        await env.send_biz_event(
            {
                "event_id": "call_1",
                "seq": 1,
                "block_id": "call_1",
                "status": "done",
                "presentation": {"card_type": "tool", "caption": "写文件", "body": "…"},
                "started_at": 1.0,
            }
        )
        await env.send_biz_event(
            {
                "event_id": "call_1:result",
                "seq": 2,
                "block_id": "call_1",
                "status": "done",
                "presentation": {
                    "card_type": "tool",
                    "caption": "写文件",
                    "body": "ok",
                },
                "started_at": 1.0,
                "ended_at": 2.0,
            }
        )

        async with factory() as db:
            events = await BizEventRepository(db, _TEST_IDENTITY).list_for_chat(chat_id)
            assert [e["event_id"] for e in events] == ["call_1", "call_1:result"]
            appended = await ChatEventRepository(db, _TEST_IDENTITY).read_after(
                chat_id, -1
            )
            assert [obj.object for obj in appended] == ["biz_event", "biz_event"]

        # 同 event_id 再发一次会覆盖旧内容，行数不增加。
        await env.send_biz_event(
            {
                "event_id": "call_1",
                "seq": 1,
                "block_id": "call_1",
                "status": "error",
                "presentation": {
                    "card_type": "tool",
                    "caption": "写文件",
                    "body": "未结束",
                },
                "started_at": 1.0,
            }
        )
        async with factory() as db:
            events = await BizEventRepository(db, _TEST_IDENTITY).list_for_chat(chat_id)
            assert len(events) == 2
            assert events[0]["status"] == "error"

        # 坏 payload 只记日志，不抛回算法 worker。
        await env.send_biz_event({"bad": "payload"})

    asyncio.run(scenario())


def test_send_segment_persists_five_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        env = await _make_envelope(session_id, chat_id)
        await env.send_segment(
            {
                "segment_id": "seg_0001",
                "title": "取数",
                "input": "dws_qwen_chat_1d",
                "behavior": "查询近 30 天 DAU",
                "conclusion": "环比上升",
                "artifact": [
                    {
                        "name": "dau.csv",
                        "description": "结果",
                        "relative_path": "a/dau.csv",
                    }
                ],
                "coverage": {"start_seq": 1, "end_seq": 2},
                "started_at": 1.0,
                "ended_at": 2.0,
            }
        )
        async with factory() as db:
            segs = (
                await SegmentRepository(db, _TEST_IDENTITY).list_for_chats([chat_id])
            )[chat_id]
            assert len(segs) == 1
            seg = segs[0]
            assert seg["segment_id"] == "seg_0001"
            assert seg["coverage"] == {"start_seq": 1, "end_seq": 2}
            assert seg["artifact"][0]["relative_path"] == "a/dau.csv"
            assert seg["confidence"] is None

    asyncio.run(scenario())


def test_send_segment_persists_confidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        env = await _make_envelope(session_id, chat_id)
        confidence = {
            "level": "medium",
            "summary": "口径未经用户确认",
        }
        await env.send_segment(
            {
                "segment_id": "seg_0002",
                "title": "取数",
                "input": "dws_qwen_chat_1d",
                "behavior": "查询近 30 天 DAU",
                "conclusion": "环比上升",
                "artifact": [
                    {
                        "name": "dau.csv",
                        "description": "结果",
                        "relative_path": "a/dau.csv",
                    }
                ],
                "coverage": {"start_seq": 1, "end_seq": 2},
                "started_at": 1.0,
                "ended_at": 2.0,
                "confidence": confidence,
            }
        )
        async with factory() as db:
            segs = (
                await SegmentRepository(db, _TEST_IDENTITY).list_for_chats([chat_id])
            )[chat_id]
            assert segs[0]["confidence"] == confidence

    asyncio.run(scenario())


def test_send_followup_persists_and_emits(tmp_path: Path) -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        env = await _make_envelope(session_id, chat_id)
        questions = ["拆到一级类目看贡献？", "对比去年同期？"]
        await env.send_followup(questions)

        async with factory() as db:
            rows = await FollowUpRepository(db, _TEST_IDENTITY).list_for_chats(
                [chat_id]
            )
            assert rows[chat_id] == {"chat_id": chat_id, "questions": questions}
            appended = await ChatEventRepository(db, _TEST_IDENTITY).read_after(
                chat_id, -1
            )
            assert [obj.object for obj in appended] == ["followup.generated"]
            assert appended[0].followup.questions == questions

    asyncio.run(scenario())


def test_list_session_files_filters_hidden_and_skills(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "analysis").mkdir(parents=True)
    (root / "analysis" / "dau.csv").write_text("a,b\n")
    (root / ".mcp").write_text("{}")
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text("x")

    items = list_session_files(root)
    assert [i["rel_path"] for i in items] == ["analysis/dau.csv"]
    assert items[0]["mime_type"] == "text/csv"
    assert items[0]["size_bytes"] > 0

    assert list_session_files(tmp_path / "missing") == []
