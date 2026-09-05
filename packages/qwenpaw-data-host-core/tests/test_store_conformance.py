# -*- coding: utf-8 -*-
"""Protocol conformance suite: every test runs against both store backends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.store.json_store import (
    JSONChatEventStore,
    JSONChatStore,
    JSONSessionStore,
)

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import (  # noqa: E402
    SQLChatEventStore,
    SQLChatStore,
    SQLSessionStore,
)


class Backend:
    def __init__(self, name: str, sessions, chats, events, factory=None) -> None:
        self.name = name
        self.sessions = sessions
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
        b = Backend(
            "json",
            JSONSessionStore(tmp_path),
            JSONChatStore(tmp_path),
            JSONChatEventStore(tmp_path),
        )
        b._root = tmp_path
        yield b
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    await init_db(engine)
    b = Backend(
        "sql",
        SQLSessionStore(factory),
        SQLChatStore(factory),
        SQLChatEventStore(factory),
        factory,
    )
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


# ---- SessionStore conformance ----


def _create_session(title: str = "Q3 分析", **kwargs) -> Session:
    return Session.create(identity=Identity.anonymous(), title=title, **kwargs)


async def test_session_roundtrip_and_rename(backend: Backend) -> None:
    session = _create_session()
    await backend.sessions.add(session)

    loaded = await backend.sessions.get(session.id)
    assert loaded.title == "Q3 分析"
    assert loaded.chat_count == 0
    assert loaded.identity.user_id == "local"

    loaded.rename("改名了")
    await backend.sessions.save(loaded)
    assert (await backend.sessions.get(session.id)).title == "改名了"

    with pytest.raises(RuntimeError, match="CONFLICT"):
        await backend.sessions.add(session)
    with pytest.raises(LookupError):
        await backend.sessions.get("ses_missing")


async def test_session_open_chat_and_active_tracking(backend: Backend) -> None:
    session = _create_session()
    await backend.sessions.add(session)
    assert not await backend.sessions.has_active_chat(session.id)

    chat = session.open_chat(
        text="hi",
        datasource_id="ds1",
        has_active_chat=False,
    )
    await backend.sessions.save(session)
    await backend.chats.add(chat)

    assert chat.sequence == 1
    assert (await backend.sessions.get(session.id)).chat_count == 1
    assert (await backend.sessions.get(session.id)).datasource_id == "ds1"
    assert await backend.sessions.has_active_chat(session.id)

    with pytest.raises(RuntimeError, match="CONFLICT"):
        session.open_chat(text="again", datasource_id=None, has_active_chat=True)


async def test_session_list_filters_and_pagination(backend: Backend) -> None:
    s1 = _create_session(title="销售分析")
    s2 = _create_session(title="库存巡检")
    s3 = _create_session(title="销售归因")
    for s in (s1, s2, s3):
        await backend.sessions.add(s)

    # running filter: give s2 an active chat
    chat = s2.open_chat(text="run", datasource_id=None, has_active_chat=False)
    await backend.sessions.save(s2)
    await backend.chats.add(chat)

    items, total = await backend.sessions.list(search_text="销售")
    assert total == 2
    assert {s.id for s, _ in items} == {s1.id, s3.id}

    running, total_running = await backend.sessions.list(status="running")
    assert total_running == 1 and running[0][0].id == s2.id
    assert running[0][1] is True

    paged, total_all = await backend.sessions.list(page=1, page_size=2)
    assert total_all == 3 and len(paged) == 2


async def test_session_soft_delete(backend: Backend) -> None:
    session = _create_session()
    await backend.sessions.add(session)

    await backend.sessions.delete(session.id)
    with pytest.raises(LookupError):
        await backend.sessions.get(session.id)
    _, total = await backend.sessions.list()
    assert total == 0
    # idempotent
    await backend.sessions.delete(session.id)


async def test_session_delete_blocked_by_active_chat(backend: Backend) -> None:
    session = _create_session()
    await backend.sessions.add(session)
    chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
    await backend.sessions.save(session)
    await backend.chats.add(chat)

    with pytest.raises(RuntimeError, match="CONFLICT"):
        await backend.sessions.delete(session.id)


# ---- PreferencesStore conformance ----


@pytest.fixture
def prefs_backend(backend: Backend, tmp_path: Path):
    from qwenpaw_data.host.core.store.json_store import JSONPreferencesStore
    from qwenpaw_data.host.core.store.sql_store import SQLPreferencesStore

    if backend.name == "json":
        return backend, JSONPreferencesStore(tmp_path)
    return backend, SQLPreferencesStore(backend.factory)


async def test_prefs_provider_roundtrip(prefs_backend, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_PREFS_MASTER_SECRET", raising=False)
    _, prefs = prefs_backend

    with pytest.raises(ValueError, match="api_key is required"):
        await prefs.upsert_provider("u1", "dashscope", {"base_url": "x"})
    with pytest.raises(ValueError, match="unknown provider_id"):
        await prefs.upsert_provider("u1", "nope", {"api_key": "k"})

    await prefs.upsert_provider("u1", "dashscope", {"api_key": "sk-secret"})
    loaded = await prefs.load("u1")
    assert loaded.providers["dashscope"].api_key == "sk-secret"
    assert loaded.providers["dashscope"].base_url is None

    # patch base_url only: api_key preserved
    await prefs.upsert_provider("u1", "dashscope", {"base_url": "https://p/v1"})
    loaded = await prefs.load("u1")
    assert loaded.providers["dashscope"].api_key == "sk-secret"
    assert loaded.providers["dashscope"].base_url == "https://p/v1"

    # user isolation
    assert (await prefs.load("u2")).providers == {}

    await prefs.delete_provider("u1", "dashscope")
    assert (await prefs.load("u1")).providers == {}
    with pytest.raises(LookupError):
        await prefs.delete_provider("u1", "dashscope")


async def test_prefs_encryption_at_rest(prefs_backend, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_PREFS_MASTER_SECRET", "ab" * 32)
    _, prefs = prefs_backend
    await prefs.upsert_provider("u1", "openai", {"api_key": "sk-plain"})
    loaded = await prefs.load("u1")
    assert loaded.providers["openai"].api_key == "sk-plain"


async def test_prefs_models_and_active_selection(prefs_backend, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_PREFS_MASTER_SECRET", raising=False)
    _, prefs = prefs_backend

    with pytest.raises(ValueError, match="name is required"):
        await prefs.upsert_model("u1", "dashscope", "my-model", source="extra")
    await prefs.upsert_model(
        "u1", "dashscope", "my-model", source="extra", name="My Model"
    )
    loaded = await prefs.load("u1")
    assert loaded.models[("dashscope", "my-model")].name == "My Model"

    # active selection requires configured credentials
    with pytest.raises(ValueError, match="provider is not configured"):
        await prefs.set_active_models(
            "u1",
            default_provider_id="dashscope",
            default_model_id="qwen-max",
        )
    await prefs.upsert_provider("u1", "dashscope", {"api_key": "sk-k"})
    active = await prefs.set_active_models(
        "u1",
        default_provider_id="dashscope",
        default_model_id="my-model",
    )
    assert active["default_model_id"] == "my-model"

    loaded = await prefs.load("u1")
    resolved = loaded.active_default()
    assert resolved is not None
    assert resolved.model_id == "my-model"
    assert resolved.name == "My Model"
    assert resolved.chat_model == "DashScopeChatModel"

    await prefs.delete_model("u1", "dashscope", "my-model")
    with pytest.raises(LookupError):
        await prefs.delete_model("u1", "dashscope", "my-model")


# ---- CronStore conformance ----


@pytest.fixture
def cron_backend(backend: Backend, tmp_path: Path):
    from qwenpaw_data.host.core.store.json_store import JSONCronStore
    from qwenpaw_data.host.core.store.sql_store import SQLCronStore

    if backend.name == "json":
        return JSONCronStore(tmp_path)
    return SQLCronStore(backend.factory)


def _job_body(name: str = "每日晨报", **overrides):
    from qwenpaw_data.host.core.api.models.cron import CronJobWrite

    values = {
        "name": name,
        "message": "生成日报",
        "datasource_id": "ds1",
        "schedule": {"type": "cron", "cron": "0 8 * * 1"},
    }
    values.update(overrides)
    return CronJobWrite.model_validate(values)


async def test_cron_crud_roundtrip(cron_backend) -> None:
    store = cron_backend
    job = await store.create("u1", _job_body())
    assert job["enabled"] is True
    assert job["schedule"]["cron"] == "0 8 * * mon"  # dow normalized

    got = await store.get("u1", job["id"])
    assert got["name"] == "每日晨报"

    replaced = await store.replace(
        "u1", job["id"], _job_body(name="改名", enabled=False)
    )
    assert replaced["name"] == "改名" and replaced["enabled"] is False

    paused = await store.set_enabled("u1", job["id"], True)
    assert paused["enabled"] is True

    listing = await store.list("u1")
    assert [j["id"] for j in listing] == [job["id"]]

    # user isolation + unscoped scheduler paths
    with pytest.raises(LookupError):
        await store.get("u2", job["id"])
    assert (await store.get_by_id(job["id"]))["id"] == job["id"]
    assert [j["id"] for j in await store.list_all()] == [job["id"]]

    await store.delete("u1", job["id"])
    with pytest.raises(LookupError):
        await store.get("u1", job["id"])
    with pytest.raises(LookupError):
        await store.get_by_id(job["id"])


# --- settlement cards -------------------------------------------------------

from qwenpaw_data.host.core.store.json_store import JSONSettlementStore  # noqa: E402
from qwenpaw_data.host.core.store.sql_store import SQLSettlementStore  # noqa: E402


@pytest.fixture(params=["json", "sql"])
async def settlement_backend(request, tmp_path: Path):
    if request.param == "json":
        yield JSONSettlementStore(tmp_path)
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'settlement.db'}",
    )
    await init_db(engine)
    yield SQLSettlementStore(factory)
    await engine.dispose()


_CARD_FIELDS = {
    "metric_name": "GMV",
    "caliber": "支付金额",
    "domain": "交易",
    "table": "orders",
    "formula_sql": "SELECT SUM(amount)",
}


async def _add_card(store, *, user_id="local", session_id="sess1", **overrides):
    kwargs = {
        "user_id": user_id,
        "session_id": session_id,
        "source_chat_id": "chat_1",
        "type": "metric_caliber",
        "fields": dict(_CARD_FIELDS),
    }
    kwargs.update(overrides)
    return await store.add(**kwargs)


async def test_settlement_add_and_list_desc(settlement_backend) -> None:
    store = settlement_backend
    first = await _add_card(store)
    await asyncio.sleep(0.01)
    second = await _add_card(store, type="dimension_def", fields={"dimension_name": "渠道"})

    cards = await store.list_by_session("local", "sess1")
    assert [c["id"] for c in cards] == [second["id"], first["id"]]
    assert cards[0]["fields"] == {"dimension_name": "渠道"}
    assert cards[1]["status"] == "pending"
    assert cards[1]["created_at"] is not None

    only_metric = await store.list_by_session("local", "sess1", status="pending")
    assert len(only_metric) == 2
    assert await store.list_by_session("local", "sess1", status="confirmed") == []
    assert await store.list_by_session("other", "sess1") == []
    assert await store.list_by_session("local", "sess2") == []


async def test_settlement_get_scoping(settlement_backend) -> None:
    store = settlement_backend
    card = await _add_card(store)

    loaded = await store.get("local", card["id"], session_id="sess1")
    assert loaded["id"] == card["id"]

    with pytest.raises(LookupError):
        await store.get("other", card["id"], session_id="sess1")
    with pytest.raises(LookupError):
        await store.get("local", card["id"], session_id="sess2")
    with pytest.raises(LookupError):
        await store.get("local", "card_missing", session_id="sess1")


async def test_settlement_mark_queried_only_pending(settlement_backend) -> None:
    store = settlement_backend
    pending = await _add_card(store)
    confirmed = await _add_card(store)
    await store.confirm("local", confirmed["id"], session_id="sess1")

    await store.mark_queried(
        "local", "sess1", [pending["id"], confirmed["id"], "card_missing"]
    )
    assert (await store.get("local", pending["id"], session_id="sess1"))[
        "status"
    ] == "queried"
    assert (await store.get("local", confirmed["id"], session_id="sess1"))[
        "status"
    ] == "confirmed"


async def test_settlement_confirm_and_dismiss(settlement_backend) -> None:
    store = settlement_backend
    card = await _add_card(store)

    new_fields = {**_CARD_FIELDS, "caliber": "含退款"}
    confirmed = await store.confirm(
        "local", card["id"], session_id="sess1", fields=new_fields
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["fields"] == new_fields
    assert confirmed["confirmed_at"] is not None

    with pytest.raises(ValueError):
        await store.dismiss("local", card["id"], session_id="sess1")
    with pytest.raises(ValueError):
        await store.confirm("local", card["id"], session_id="sess1")

    other = await _add_card(store)
    await store.mark_queried("local", "sess1", [other["id"]])
    dismissed = await store.dismiss("local", other["id"], session_id="sess1")
    assert dismissed["status"] == "dismissed"


async def test_settlement_delete_if_unconfirmed(settlement_backend) -> None:
    store = settlement_backend
    card = await _add_card(store)

    assert await store.delete_if_unconfirmed(
        "other", card["id"], session_id="sess1"
    ) is False
    assert await store.delete_if_unconfirmed(
        "local", card["id"], session_id="sess2"
    ) is False
    assert await store.delete_if_unconfirmed(
        "local", card["id"], session_id="sess1"
    ) is True
    assert await store.delete_if_unconfirmed(
        "local", card["id"], session_id="sess1"
    ) is False

    confirmed = await _add_card(store)
    await store.confirm("local", confirmed["id"], session_id="sess1")
    assert await store.delete_if_unconfirmed(
        "local", confirmed["id"], session_id="sess1"
    ) is False
