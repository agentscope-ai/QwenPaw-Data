# -*- coding: utf-8 -*-
"""Protocol conformance suite: every test runs against both store backends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.json_store import (
    JSONChatEventStore,
    JSONChatStore,
)

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import (  # noqa: E402
    SQLChatEventStore,
    SQLChatStore,
)


class Backend:
    def __init__(self, name: str, chats, events, factory=None) -> None:
        self.name = name
        self.chats = chats
        self.events = events
        self.factory = factory

    def reopen_event_store(self):
        if self.name == "json":
            return JSONChatEventStore(self._root)
        return SQLChatEventStore(self.factory)


@pytest.fixture(params=["json", "sql"])
async def backend(request, tmp_path: Path):
    if request.param == "json":
        b = Backend("json", JSONChatStore(tmp_path), JSONChatEventStore(tmp_path))
        b._root = tmp_path
        yield b
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    await init_db(engine)
    b = Backend("sql", SQLChatStore(factory), SQLChatEventStore(factory), factory)
    yield b
    await engine.dispose()


def _start_chat(session_id: str = "sess1", sequence: int = 1) -> Chat:
    return Chat.start(
        session_id=session_id,
        identity=Identity.anonymous(),
        sequence=sequence,
        datasource_id="ds1",
        text="hello",
    )


async def _add_chat(backend: Backend, **kwargs) -> Chat:
    chat = _start_chat(**kwargs)
    await backend.chats.add(chat)
    return chat


async def test_add_get_save_roundtrip(backend: Backend) -> None:
    chat = await _add_chat(backend)

    loaded = await backend.chats.get(chat.id)
    assert loaded.id == chat.id
    assert loaded.session_id == "sess1"
    assert loaded.identity.user_id == "local"
    assert loaded.status == "running"
    assert loaded.plan is None

    loaded.mark_status("completed")
    await backend.chats.save(loaded)
    again = await backend.chats.get(chat.id)
    assert again.status == "completed"
    assert again.completed_at is not None


async def test_add_conflicts_on_duplicate(backend: Backend) -> None:
    chat = await _add_chat(backend)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await backend.chats.add(chat)


async def test_get_scoped_by_session(backend: Backend) -> None:
    chat = await _add_chat(backend)

    assert (await backend.chats.get(chat.id, session_id="sess1")).id == chat.id
    with pytest.raises(LookupError):
        await backend.chats.get(chat.id, session_id="other")
    with pytest.raises(LookupError):
        await backend.chats.get("chat_missing")


async def test_active_listing_and_session_ordering(backend: Backend) -> None:
    first = await _add_chat(backend, sequence=1)
    second = await _add_chat(backend, sequence=2)
    other = await _add_chat(backend, session_id="sess2", sequence=1)

    first_loaded = await backend.chats.get(first.id)
    first_loaded.mark_status("completed")
    await backend.chats.save(first_loaded)

    listed = await backend.chats.list_for_session("sess1")
    assert [c.sequence for c in listed] == [1, 2]

    active = await backend.chats.get_active_for_session("sess1")
    assert active is not None and active.id == second.id

    all_active = await backend.chats.list_active()
    assert {c.id for c in all_active} == {second.id, other.id}


async def test_update_plan(backend: Backend) -> None:
    chat = await _add_chat(backend)
    await backend.chats.update_plan(chat.id, {"nodes": [{"id": "n1"}]})
    loaded = await backend.chats.get(chat.id)
    assert loaded.plan == {"nodes": [{"id": "n1"}]}


async def test_event_watermark_reload(backend: Backend) -> None:
    chat = await _add_chat(backend)
    assert chat.last_sequence_number == -1

    await backend.chats.reload_event_watermark(chat)
    assert chat.last_sequence_number == -1

    for _ in range(3):
        await backend.events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={"object": "response", "id": "resp_1", "status": "in_progress"},
        )
    await backend.chats.reload_event_watermark(chat)
    assert chat.last_sequence_number == 2


async def test_event_append_read_after(backend: Backend) -> None:
    chat = await _add_chat(backend)
    for i in range(5):
        obj = await backend.events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={"object": "response", "id": f"resp_{i}", "status": "created"},
        )
        assert obj.sequence_number == i

    tail = await backend.events.read_after(chat.id, 2)
    assert [e.sequence_number for e in tail] == [3, 4]
    assert await backend.events.last_sequence_number(chat.id) == 4
    assert await backend.events.read_after("chat_missing", -1) == []
    assert await backend.events.last_sequence_number("chat_missing") == -1


async def test_event_store_rejects_invalid_payload(backend: Backend) -> None:
    chat = await _add_chat(backend)
    with pytest.raises(ValueError):
        await backend.events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={"object": "mystery"},
        )
    assert await backend.events.last_sequence_number(chat.id) == -1


async def test_event_counter_survives_new_instance(backend: Backend) -> None:
    chat = await _add_chat(backend)
    await backend.events.append(
        session_id=chat.session_id,
        chat_id=chat.id,
        payload={"object": "response", "id": "resp_0", "status": "created"},
    )

    reopened = backend.reopen_event_store()
    obj = await reopened.append(
        session_id=chat.session_id,
        chat_id=chat.id,
        payload={"object": "response", "id": "resp_1", "status": "completed"},
    )
    assert obj.sequence_number == 1


async def test_concurrent_appends_dense_monotonic(backend: Backend) -> None:
    chat = await _add_chat(backend)
    total = 30

    async def _append(i: int) -> int:
        obj = await backend.events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={
                "object": "content",
                "msg_id": "msg_1",
                "type": "text",
                "delta": True,
                "text": f"chunk-{i}",
            },
        )
        return obj.sequence_number

    sequences = await asyncio.gather(*(_append(i) for i in range(total)))
    assert sorted(sequences) == list(range(total))

    persisted = await backend.events.read_after(chat.id, -1)
    assert [e.sequence_number for e in persisted] == list(range(total))
    assert {e.text for e in persisted} == {f"chunk-{i}" for i in range(total)}


async def test_concurrent_appends_across_chats_independent(backend: Backend) -> None:
    chat_a = await _add_chat(backend, sequence=1)
    chat_b = await _add_chat(backend, session_id="sess2", sequence=1)

    async def _append(chat: Chat, i: int) -> int:
        obj = await backend.events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={"object": "response", "id": f"resp_{i}", "status": "created"},
        )
        return obj.sequence_number

    results = await asyncio.gather(
        *(_append(chat_a, i) for i in range(10)),
        *(_append(chat_b, i) for i in range(10)),
    )
    assert sorted(results[:10]) == list(range(10))
    assert sorted(results[10:]) == list(range(10))
    assert await backend.events.last_sequence_number(chat_a.id) == 9
    assert await backend.events.last_sequence_number(chat_b.id) == 9
