"""Cancelled/failed transactions must retain the exact stream batch for retry."""

import asyncio

import pytest

from qwenpaw_data.host.core.db.engine import get_session_factory, reset_engine
from qwenpaw_data.host.core.db.tables import ChatRow
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.stream.hub import get_hub

from .test_output_stream_persist import _prepare_db, _IDENTITY


@pytest.mark.parametrize("stage", ["before_write", "after_write"])
async def test_cancelled_flush_retains_batch_and_can_retry(stage, monkeypatch):
    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    await stream.text_delta(msg_id="message", index=0, text="retained")
    reached = asyncio.Event()
    original = ChatEventRepository.persist_batch

    async def interrupt(self, records):
        if stage == "after_write":
            await original(self, records)
        reached.set()
        await asyncio.Event().wait()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ChatEventRepository, "persist_batch", interrupt)
            task = asyncio.create_task(stream.flush())
            await asyncio.wait_for(reached.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with factory() as db:
            assert (
                await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1) == []
            )
            assert (await db.get(ChatRow, chat_id)).last_sequence_number == -1
        await stream.flush()
        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            assert [(e.sequence_number, e.text) for e in events] == [(0, "retained")]
            assert (await db.get(ChatRow, chat_id)).last_sequence_number == 0
        await stream.text_delta(msg_id="message", index=0, text="continued")
        await stream.flush()
        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            assert [e.sequence_number for e in events] == [0, 1]
    finally:
        await reset_engine()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_terminal_cleanup_is_retried_with_its_batch(monkeypatch, cancel):
    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    await stream.text_delta(msg_id="message", index=0, text="ephemeral")
    queue = get_hub().attach(chat_id)
    reached = asyncio.Event()

    async def fail_cleanup(self, selected):
        assert selected == chat_id
        reached.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic cleanup failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ChatEventRepository, "delete_chat_content", fail_cleanup)
            task = asyncio.create_task(stream.response_completed())
            await asyncio.wait_for(reached.wait(), timeout=2)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await task
        async with factory() as db:
            assert (
                await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1) == []
            )
        await stream.flush()
        published = queue.get_nowait()
        assert published.object == "response" and published.status == "completed"
        assert queue.empty()
        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            assert [(e.object, e.sequence_number) for e in events] == [("response", 1)]
            assert events[0].status == "completed"
            assert (await db.get(ChatRow, chat_id)).last_sequence_number == 1
    finally:
        get_hub().detach(chat_id, queue)
        await reset_engine()


@pytest.mark.parametrize("cancel", [False, True])
async def test_committed_event_publication_retry_never_reinserts_sql(
    monkeypatch, cancel
):
    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    original = ChatEventRepository.persist_batch
    writes = []
    reached = asyncio.Event()
    queue = get_hub().attach(chat_id)

    async def track(self, batch):
        writes.append([r.sequence_number for r in batch])
        return await original(self, batch)

    async def interrupted(*args):
        reached.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic publish failure")

    monkeypatch.setattr(ChatEventRepository, "persist_batch", track)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(stream.hub, "publish", interrupted)
            task = asyncio.create_task(stream.response_created())
            await asyncio.wait_for(reached.wait(), timeout=2)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await task
        async with factory() as db:
            assert (
                len(await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1))
                == 1
            )
        await stream.flush()
        assert writes == [[0]]
        assert queue.get_nowait().sequence_number == 0 and queue.empty()
        await stream.response_in_progress()
        assert queue.get_nowait().sequence_number == 1 and queue.empty()
        assert writes == [[0], [1]]
    finally:
        get_hub().detach(chat_id, queue)
        await reset_engine()


async def test_known_commit_survives_cancellation_during_session_close():
    from contextlib import asynccontextmanager

    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    opened = []

    @asynccontextmanager
    async def cancelled_close():
        opened.append(True)
        async with factory() as db:
            yield db
        raise asyncio.CancelledError()

    stream = OutputStream(
        cancelled_close,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    queue = get_hub().attach(chat_id)
    try:
        with pytest.raises(asyncio.CancelledError):
            await stream.response_created()
        await stream.flush()  # No session needed: commit was positively observed.
        assert opened == [True]
        assert queue.get_nowait().sequence_number == 0 and queue.empty()
        async with factory() as db:
            assert (
                len(await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1))
                == 1
            )
    finally:
        get_hub().detach(chat_id, queue)
        await reset_engine()


async def test_next_append_retries_failed_batch_before_assigning_new_sequence(
    monkeypatch,
):
    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    queue = get_hub().attach(chat_id)

    async def fail(self, batch):
        raise RuntimeError("synthetic failure before write")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ChatEventRepository, "persist_batch", fail)
            with pytest.raises(RuntimeError):
                await stream.response_created()
        await stream.text_delta(msg_id="message", index=0, text="next")
        assert [queue.get_nowait().sequence_number for _ in range(2)] == [0, 1]
        assert queue.empty()
        async with factory() as db:
            # Keep internal batching: the new delta is live but still buffered.
            assert [
                e.sequence_number
                for e in await ChatEventRepository(db, _IDENTITY).read_after(
                    chat_id, -1
                )
            ] == [0]
        await stream.flush()
        async with factory() as db:
            assert [
                e.sequence_number
                for e in await ChatEventRepository(db, _IDENTITY).read_after(
                    chat_id, -1
                )
            ] == [0, 1]
    finally:
        get_hub().detach(chat_id, queue)
        await reset_engine()
