# -*- coding: utf-8 -*-
"""并发写 chat_events 的序号分配：主轮次与算法 worker 各持一个 DB 会话。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
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
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

_EVENTS_PER_WRITER = 20
_IDENTITY = Identity(
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


def _stream(
    factory: Any,
    session_id: str,
    chat_id: str,
    last_sequence_number: int = -1,
) -> OutputStream:
    return OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=last_sequence_number,
    )


def test_concurrent_writers_get_unique_sequence_numbers(tmp_path: Path) -> None:
    """两个 DB 会话先后 append 终态，序号必须全局唯一且 last_sequence_number 推进到最大值。"""

    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()

        async def writer(tag: str) -> None:
            async with factory() as db:
                chat = await ChatRepository(db, _IDENTITY).get(chat_id)
            stream = _stream(factory, session_id, chat_id, chat.last_sequence_number)
            for i in range(_EVENTS_PER_WRITER):
                await stream.message_complete(
                    msg_id=f"msg_{tag}_{i}",
                    sequence=i + 1,
                    type="message",
                    role="assistant",
                    content=[
                        {"object": "content", "type": "text", "text": f"{tag}-{i}"}
                    ],
                )

        await writer("main")
        await writer("worker")

        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            seqs = [obj.sequence_number for obj in events]
            total = 2 * _EVENTS_PER_WRITER
            assert len(seqs) == total
            assert seqs == list(range(total))
            chat = await ChatRepository(db, _IDENTITY).get(chat_id)
            assert chat.last_sequence_number == total - 1

    asyncio.run(scenario())


def test_chat_save_does_not_clobber_watermark(tmp_path: Path) -> None:
    """主会话持有旧 Chat 期间，另一会话推进序号；save 后 last_sequence_number 不能回退。"""

    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()

        async with factory() as db:
            chat_repo = ChatRepository(db, _IDENTITY)
            chat = await chat_repo.get(chat_id)

            # 另一个会话（算法 worker）推进序号并提交。
            stream = _stream(factory, session_id, chat_id, chat.last_sequence_number)
            await stream.message_complete(
                msg_id="msg_w",
                sequence=1,
                type="message",
                role="assistant",
                content=[{"object": "content", "type": "text", "text": "w"}],
            )

            chat.mark_status("running")
            await chat_repo.reload_event_watermark(chat)
            await chat_repo.save(chat)
            await db.commit()

        async with factory() as db:
            chat = await ChatRepository(db, _IDENTITY).get(chat_id)
            assert chat.last_sequence_number == 0

    asyncio.run(scenario())


# TODO(seq-race): 这个用例红的是生产缺陷，不是测试陈旧；先记账，暂不改 src。
#
# 根因：sequence_number 的分配是"先读后加"，不是 DB 侧原子自增。
#   - stream/output_stream.py:286 `_next_seq()` 读 chats.last_sequence_number 后在
#     Python 里 +1，真正的写发生在 flush() → repositories/chat_event.py:36
#     `persist_batch()` 里，读与写分处两个事务；
#   - `_seq` 一旦初始化就不再重读水位（output_stream.py:46），所以长命的 turn 流
#     永远看不到别的写者推进过的水位；
#   - 唯一性由 (chat_id, sequence_number) 复合主键保证（db/tables.py:177）。
# 结论：同一个 chat 只要存在两个 OutputStream（即两个 DB 会话），必然分配出相同序号。
#
# 生产触发路径（都不是测试编出来的）：
#   - repositories/chat.py:189 `ChatRepository.stop()` 的心跳陈旧分支会新建
#     OutputStream 写 response_cancelled，而那个 turn 可能还在跑；
#   - channels/base.py:1257 `_force_failed_if_running()`；
#   - 判据 `ChatDomain.is_heartbeat_fresh()`（domain/chat.py:94）用本地 utcnow() 比
#     别的 POD 写入的 heartbeat_at，跨 POD 时钟偏差、heartbeat 写入被吞
#     （runtime/chat_runtime.py:128-137）都会把活着的 chat 误判为死掉。
#
# 放大效应：一次撞车之后 flush() 把批次原样塞回 _pending（output_stream.py:58-60）
# 而没人 rollback，session 进入 PendingRollbackError 状态，此后该轮每个 append 都
# 失败，日志里的根因（UNIQUE constraint）被"事务已回滚"掩盖；turn 以
# PendingRollbackError 收场，_force_failed_if_running 又会吞掉异常，chat 卡在 running。
#
# 修的方向（本用例的断言就是修好后必须成立的不变量，别放松）：
#   - 分配改到 DB 侧原子化：chats 行 `UPDATE ... last_sequence_number + n RETURNING`
#     （块预留 hi/lo 更省锁），或保留乐观分配但在 SAVEPOINT 内重编号重试；
#   - flush() 失败不能原样重塞毒批次；persist_batch 回写水位要取 max 而非批次末值。
# 修好后请删掉下面的 xfail —— strict=True 会以 XPASS 提醒。
@pytest.mark.xfail(
    strict=True,
    reason=(
        "real race: two OutputStreams on one chat both read the same watermark and "
        "allocate the same sequence_number (see TODO(seq-race) above)"
    ),
)
def test_upsert_then_append_does_not_deadlock_with_concurrent_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 先 flush 领域行再 append 时，主循环同时 append 不得锁死。

    锁死那一项已经满足（两边都能走完），当前失败点是序号唯一性，即上面的 xfail。
    """

    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        flushed = asyncio.Event()
        original = BizEventRepository.upsert

        async def delayed_upsert(self, **kwargs):
            result = await original(self, **kwargs)
            flushed.set()
            await asyncio.sleep(0.05)
            return result

        monkeypatch.setattr(BizEventRepository, "upsert", delayed_upsert)

        async def worker() -> None:
            await _stream(factory, session_id, chat_id).biz_event(
                event_id="call_1",
                seq=1,
                presentation={"card_type": "tool", "caption": "x", "body": "y"},
                started_at=1.0,
            )

        async def main() -> None:
            await flushed.wait()
            await _stream(factory, session_id, chat_id).message_complete(
                msg_id="msg_main",
                sequence=1,
                type="message",
                role="assistant",
                content=[{"object": "content", "type": "text", "text": "m"}],
            )

        await asyncio.wait_for(asyncio.gather(worker(), main()), timeout=2)

        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            assert [obj.object for obj in events] == ["message"]
            assert events[0].sequence_number == 0
            chat = await ChatRepository(db, _IDENTITY).get(chat_id)
            assert chat.last_sequence_number == 0

    asyncio.run(scenario())


def test_trace_restores_only_failed_mcp_connections_in_order(tmp_path: Path) -> None:
    from qwenpaw_data.host.core.api.routers.trace import trace

    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        stream = _stream(factory, session_id, chat_id)
        await stream.mcp_connect(mcp_name="ok", status="connected")
        await stream.mcp_connect(mcp_name="failed-1", status="failed", error="refused")
        await stream.mcp_connect(mcp_name="failed-2", status="failed", error="timeout")
        await stream.message_complete(
            msg_id="msg_1",
            sequence=1,
            type="message",
            role="assistant",
            content=[{"object": "content", "type": "text", "text": "hello"}],
        )
        async with factory() as db:
            result = await trace(
                session_id, chat_id, identity=_IDENTITY, db=db, view="raw"
            )
            assert [event["mcp_name"] for event in result["mcp_connections"]] == [
                "failed-1",
                "failed-2",
            ]
            assert [
                event["sequence_number"] for event in result["mcp_connections"]
            ] == [1, 2]
            assert len(result["messages"]) == 1
            assert (
                await ChatEventRepository(db, _IDENTITY).read_mcp_connections("other")
                == []
            )
            business = await trace(
                session_id, chat_id, identity=_IDENTITY, db=db, view="business"
            )
            assert business["mcp_connections"] == []
        await reset_engine()

    asyncio.run(scenario())
