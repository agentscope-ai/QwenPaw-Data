from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.json_store import (
    JSONChatEventStore,
    JSONChatStore,
)


def _start_chat(session_id: str = "sess1", sequence: int = 1) -> Chat:
    return Chat.start(
        session_id=session_id,
        identity=Identity.anonymous(),
        sequence=sequence,
        datasource_id="ds1",
        text="hello",
    )


async def test_add_get_save_roundtrip(tmp_path: Path) -> None:
    store = JSONChatStore(tmp_path)
    chat = _start_chat()
    await store.add(chat)

    loaded = await store.get(chat.id)
    assert loaded.id == chat.id
    assert loaded.session_id == "sess1"
    assert loaded.identity.user_id == "local"
    assert loaded.status == "running"
    assert loaded.plan is None
    assert loaded.started_at == chat.started_at

    loaded.mark_status("completed")
    await store.save(loaded)
    again = await store.get(chat.id)
    assert again.status == "completed"
    assert again.completed_at is not None


async def test_add_conflicts_on_duplicate(tmp_path: Path) -> None:
    store = JSONChatStore(tmp_path)
    chat = _start_chat()
    await store.add(chat)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await store.add(chat)


async def test_get_scoped_by_session(tmp_path: Path) -> None:
    store = JSONChatStore(tmp_path)
    chat = _start_chat()
    await store.add(chat)

    assert (await store.get(chat.id, session_id="sess1")).id == chat.id
    with pytest.raises(LookupError):
        await store.get(chat.id, session_id="other")
    with pytest.raises(LookupError):
        await store.get("chat_missing")


async def test_active_listing_and_session_ordering(tmp_path: Path) -> None:
    store = JSONChatStore(tmp_path)
    first = _start_chat(sequence=1)
    second = _start_chat(sequence=2)
    other = _start_chat(session_id="sess2", sequence=1)
    first.mark_status("completed")
    for chat in (first, second, other):
        await store.add(chat)

    listed = await store.list_for_session("sess1")
    assert [c.sequence for c in listed] == [1, 2]

    active = await store.get_active_for_session("sess1")
    assert active is not None and active.id == second.id

    all_active = await store.list_active()
    assert {c.id for c in all_active} == {second.id, other.id}


async def test_update_plan(tmp_path: Path) -> None:
    store = JSONChatStore(tmp_path)
    chat = _start_chat()
    await store.add(chat)
    await store.update_plan(chat.id, {"nodes": [{"id": "n1"}]})
    loaded = await store.get(chat.id)
    assert loaded.plan == {"nodes": [{"id": "n1"}]}


async def test_event_watermark_reload(tmp_path: Path) -> None:
    chats = JSONChatStore(tmp_path)
    events = JSONChatEventStore(tmp_path)
    chat = _start_chat()
    await chats.add(chat)
    assert chat.last_sequence_number == -1

    await chats.reload_event_watermark(chat)
    assert chat.last_sequence_number == -1

    for _ in range(3):
        await events.append(
            session_id=chat.session_id,
            chat_id=chat.id,
            payload={"object": "response", "id": "resp_1", "status": "in_progress"},
        )
    await chats.reload_event_watermark(chat)
    assert chat.last_sequence_number == 2


async def test_event_append_read_after(tmp_path: Path) -> None:
    events = JSONChatEventStore(tmp_path)
    for i in range(5):
        obj = await events.append(
            session_id="sess1",
            chat_id="chat_x",
            payload={"object": "response", "id": f"resp_{i}", "status": "created"},
        )
        assert obj.sequence_number == i

    tail = await events.read_after("chat_x", 2)
    assert [e.sequence_number for e in tail] == [3, 4]
    assert await events.last_sequence_number("chat_x") == 4
    assert await events.read_after("chat_missing", -1) == []


async def test_event_store_rejects_invalid_payload(tmp_path: Path) -> None:
    events = JSONChatEventStore(tmp_path)
    with pytest.raises(ValueError):
        await events.append(
            session_id="sess1",
            chat_id="chat_x",
            payload={"object": "mystery"},
        )
    assert await events.last_sequence_number("chat_x") == -1


async def test_event_counter_survives_new_instance(tmp_path: Path) -> None:
    events = JSONChatEventStore(tmp_path)
    await events.append(
        session_id="sess1",
        chat_id="chat_x",
        payload={"object": "response", "id": "resp_0", "status": "created"},
    )

    reopened = JSONChatEventStore(tmp_path)
    obj = await reopened.append(
        session_id="sess1",
        chat_id="chat_x",
        payload={"object": "response", "id": "resp_1", "status": "completed"},
    )
    assert obj.sequence_number == 1
