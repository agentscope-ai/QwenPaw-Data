"""Persistence contracts on the current SQL repositories and single-writer stream.

Each helper call commits and closes its session, so reads prove durability across
connections. JSON is an offline migration source, not a supported runtime backend.
Repository reads are authorized by the service's access_session boundary; a bound
Identity alone is not an authorization check for shared session/chat reads.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from agentscope.state import TaskContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.db.tables import ChatRow, CronJobRow, UserProviderRow
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.providers import provider_registry
from qwenpaw_data.host.core.repositories import (
    ChatRepository,
    PreferencesRepository,
    SessionRepository,
    SettlementRepository,
)
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.repositories.cron import CronJobRepository
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.utils.time import utcnow

from . import test_runtime_settings as settings_tests
from .conftest import TEST_IDENTITY

database = settings_tests.database


class Backend:
    def __init__(self, factory):
        self.factory = factory

    async def call(self, repository, method, *args, identity=TEST_IDENTITY, **kwargs):
        async with self.factory() as db:
            result = await getattr(repository(db, identity), method)(*args, **kwargs)
            await db.commit()
            return result

    async def writer(self, chat):
        current = await self.call(ChatRepository, "get", chat.id)
        return OutputStream(
            self.factory,
            session_id=chat.session_id,
            chat_id=chat.id,
            identity=chat.identity,
            last_sequence_number=current.last_sequence_number,
        )


@pytest.fixture
def backend(database):
    reset_hub()
    try:
        yield Backend(database)
    finally:
        reset_hub()


async def _add_session(backend, *, identity=TEST_IDENTITY, title="Q3 分析"):
    session = Session.create(identity=identity, title=title)
    await backend.call(SessionRepository, "add", session, identity=identity)
    return session


async def _add_chat(backend, *, session=None, dataset_names=None):
    session = session or await _add_session(backend)
    async with backend.factory() as db:
        sessions = SessionRepository(db, session.identity)
        current = await sessions.get(session.id)
        chat = current.open_chat(
            text="hello",
            datasource_id="ds1",
            dataset_names=dataset_names,
            has_active_chat=await sessions.has_active_chat(session.id),
        )
        await sessions.save(current)
        await ChatRepository(db, session.identity).add(chat)
        await db.commit()
    return chat


async def test_add_get_save_roundtrip(backend):
    chat = await _add_chat(backend)
    loaded = await backend.call(ChatRepository, "get", chat.id)
    assert loaded.id == chat.id and loaded.session_id == chat.session_id
    assert loaded.identity == TEST_IDENTITY
    assert loaded.status == "running" and loaded.plan == TaskContext()
    loaded.mark_status("completed")
    await backend.call(ChatRepository, "save", loaded)
    again = await backend.call(ChatRepository, "get", chat.id)
    assert again.status == "completed" and again.completed_at is not None


async def test_dataset_context_roundtrip(backend):
    chat = await _add_chat(backend, dataset_names=["sales"])
    restored = await backend.call(ChatRepository, "get", chat.id)
    # Dataset focus lives on Chat / RunContext, not in legacy TurnInput text.
    assert restored.dataset_names == ["sales"]
    assert restored.datasource_id == "ds1"


async def test_add_conflicts_on_duplicate(backend):
    chat = await _add_chat(backend)
    with pytest.raises(IntegrityError):
        await backend.call(ChatRepository, "add", chat)
    assert (await backend.call(ChatRepository, "get", chat.id)).id == chat.id


async def test_get_scoped_by_session(backend):
    chat = await _add_chat(backend)
    assert (
        await backend.call(ChatRepository, "get", chat.id, session_id=chat.session_id)
    ).id == chat.id
    for cid, sid in [(chat.id, "other"), ("missing", chat.session_id)]:
        with pytest.raises(LookupError):
            await backend.call(ChatRepository, "get", cid, session_id=sid)


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "workspace_id"])
async def test_session_access_boundary_rejects_other_identity(backend, field):
    session = await _add_session(backend)
    other = replace(TEST_IDENTITY, **{field: "other"})
    async with backend.factory() as db:
        for write in [False, True]:
            with pytest.raises(LookupError):
                await access_session(db, other, session.id, write=write)


async def test_active_listing_and_session_ordering(backend):
    session = await _add_session(backend)
    first = await _add_chat(backend, session=session)
    first.mark_status("completed")
    await backend.call(ChatRepository, "save", first)
    second = await _add_chat(backend, session=session)
    other = await _add_chat(backend)
    listed = await backend.call(ChatRepository, "list_for_session", session.id)
    assert [c.sequence for c in listed] == [1, 2]
    assert (
        await backend.call(ChatRepository, "get_active_for_session", session.id)
    ).id == second.id
    # Recovery/scheduler reads are deliberately privileged, unlike user listing.
    async with backend.factory() as db:
        active = await db.scalars(select(ChatRow.id).where(ChatRow.status == "running"))
        assert set(active) == {second.id, other.id}


async def test_update_plan(backend):
    chat = await _add_chat(backend)
    plan = TaskContext.model_validate(
        {
            "tasks": [
                {
                    "id": "1",
                    "subject": "Fetch",
                    "description": "Get evidence",
                    "metadata": {},
                }
            ]
        }
    )
    await backend.call(ChatRepository, "update_plan", chat.id, plan)
    assert (await backend.call(ChatRepository, "get", chat.id)).plan == plan


async def test_event_watermark_reload(backend):
    chat = await _add_chat(backend)
    stream = await backend.writer(chat)
    assert chat.last_sequence_number == -1
    for _ in range(3):
        await stream.response_in_progress()
    await backend.call(ChatRepository, "reload_event_watermark", chat)
    assert chat.last_sequence_number == 2


async def test_event_append_read_after(backend):
    chat = await _add_chat(backend)
    stream = await backend.writer(chat)
    for i in range(5):
        assert (await stream.response_created()).sequence_number == i
    tail = await backend.call(ChatEventRepository, "read_after", chat.id, 2)
    assert [e.sequence_number for e in tail] == [3, 4]
    assert (
        await backend.call(ChatRepository, "get", chat.id)
    ).last_sequence_number == 4
    assert await backend.call(ChatEventRepository, "read_after", "missing", -1) == []


@pytest.mark.parametrize("payload", [{}, {"object": "mystery"}, {"object": "response"}])
async def test_event_store_rejects_invalid_payload(backend, payload):
    chat = await _add_chat(backend)
    stream = await backend.writer(chat)
    with pytest.raises(ValueError):
        await stream.append(payload)
    assert (
        await backend.call(ChatRepository, "get", chat.id)
    ).last_sequence_number == -1
    assert await backend.call(ChatEventRepository, "read_after", chat.id, -1) == []
    # Rejected input must not consume a sequence number or poison the writer.
    assert (await stream.response_created()).sequence_number == 0


async def test_event_counter_survives_new_instance(backend):
    chat = await _add_chat(backend)
    stream = await backend.writer(chat)
    await stream.response_created()
    await stream.flush()
    # Sequential handoff reloads the durable watermark; not two concurrent owners.
    reopened = await backend.writer(chat)
    assert (await reopened.response_completed()).sequence_number == 1
    assert await backend.call(ChatEventRepository, "terminal_delivered", chat.id, 1)


async def test_concurrent_appends_dense_monotonic(backend):
    chat = await _add_chat(backend)
    stream = await backend.writer(chat)
    total = 30

    async def append(i):
        return (
            await stream.text_delta(msg_id="msg_1", index=0, text=f"chunk-{i}")
        ).sequence_number

    sequences = await asyncio.gather(*(append(i) for i in range(total)))
    await stream.flush()
    assert sorted(sequences) == list(range(total))
    persisted = await backend.call(ChatEventRepository, "read_after", chat.id, -1)
    assert [e.sequence_number for e in persisted] == list(range(total))
    assert {e.text for e in persisted} == {f"chunk-{i}" for i in range(total)}


async def test_concurrent_appends_across_chats_independent(backend):
    a, b = await _add_chat(backend), await _add_chat(backend)
    streams = [await backend.writer(a), await backend.writer(b)]
    results = await asyncio.gather(
        *(stream.response_created() for stream in streams for _ in range(10))
    )
    assert sorted(obj.sequence_number for obj in results[:10]) == list(range(10))
    assert sorted(obj.sequence_number for obj in results[10:]) == list(range(10))
    for chat in [a, b]:
        assert (
            await backend.call(ChatRepository, "get", chat.id)
        ).last_sequence_number == 9


async def test_session_roundtrip_and_rename(backend):
    session = await _add_session(backend)
    loaded = await backend.call(SessionRepository, "get", session.id)
    assert loaded.title == "Q3 分析" and loaded.chat_count == 0
    assert loaded.identity == TEST_IDENTITY
    loaded.rename("改名了")
    await backend.call(SessionRepository, "save", loaded)
    assert (await backend.call(SessionRepository, "get", session.id)).title == "改名了"
    with pytest.raises(IntegrityError):
        await backend.call(SessionRepository, "add", session)
    with pytest.raises(LookupError):
        await backend.call(SessionRepository, "get", "missing")


async def test_session_open_chat_and_active_tracking(backend):
    session = await _add_session(backend)
    assert not await backend.call(SessionRepository, "has_active_chat", session.id)
    chat = await _add_chat(backend, session=session)
    assert chat.sequence == 1
    loaded = await backend.call(SessionRepository, "get", session.id)
    assert loaded.chat_count == 1 and loaded.datasource_id == "ds1"
    assert await backend.call(SessionRepository, "has_active_chat", session.id)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await _add_chat(backend, session=session)
    assert (await backend.call(SessionRepository, "get", session.id)).chat_count == 1


async def test_session_list_filters_and_pagination(backend):
    s1 = await _add_session(backend, title="销售分析")
    s2 = await _add_session(backend, title="库存巡检")
    s3 = await _add_session(backend, title="销售归因")
    await _add_chat(backend, session=s2)
    items, total = await backend.call(SessionRepository, "list", search_text="销售")
    assert total == 2 and {s.id for s, _ in items} == {s1.id, s3.id}
    running, total = await backend.call(SessionRepository, "list", status="running")
    assert total == 1 and running[0][0].id == s2.id and running[0][1]
    paged, total = await backend.call(SessionRepository, "list", page=1, page_size=2)
    assert total == 3 and len(paged) == 2


async def test_session_soft_delete(backend):
    session = await _add_session(backend)
    await backend.call(SessionRepository, "delete", session.id)
    with pytest.raises(LookupError):
        await backend.call(SessionRepository, "get", session.id)
    assert await backend.call(SessionRepository, "list") == ([], 0)
    assert await backend.call(SessionRepository, "delete", session.id) is None


async def test_session_delete_blocked_by_active_chat(backend):
    session = await _add_session(backend)
    await _add_chat(backend, session=session)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await backend.call(SessionRepository, "delete", session.id)
    assert (await backend.call(SessionRepository, "get", session.id)).deleted_at is None


async def test_prefs_provider_roundtrip(backend):
    with pytest.raises(ValueError, match="api_key is required"):
        await backend.call(
            PreferencesRepository, "upsert_provider", "dashscope", {"base_url": "x"}
        )
    with pytest.raises(ValueError, match="unknown provider_id"):
        await backend.call(
            PreferencesRepository, "upsert_provider", "nope", {"api_key": "k"}
        )
    await backend.call(
        PreferencesRepository, "upsert_provider", "dashscope", {"api_key": "sk-secret"}
    )
    loaded = await backend.call(PreferencesRepository, "load")
    assert loaded.providers["dashscope"].api_key == "sk-secret"
    assert (
        loaded.providers["dashscope"].base_url
        == provider_registry.require("dashscope").base_url
    )
    await backend.call(
        PreferencesRepository,
        "upsert_provider",
        "dashscope",
        {"base_url": "https://p/v1"},
    )
    loaded = await backend.call(PreferencesRepository, "load")
    assert loaded.providers["dashscope"].api_key == "sk-secret"
    assert loaded.providers["dashscope"].base_url == "https://p/v1"
    other = replace(TEST_IDENTITY, user_id="other")
    assert (
        await backend.call(PreferencesRepository, "load", identity=other)
    ).providers == {}
    await backend.call(PreferencesRepository, "delete_provider", "dashscope")
    assert (await backend.call(PreferencesRepository, "load")).providers == {}
    with pytest.raises(LookupError):
        await backend.call(PreferencesRepository, "delete_provider", "dashscope")


async def test_prefs_encryption_at_rest(backend):
    await backend.call(
        PreferencesRepository, "upsert_provider", "openai", {"api_key": "sk-plain"}
    )
    async with backend.factory() as db:
        row = await db.get(UserProviderRow, (TEST_IDENTITY.user_id, "openai"))
        assert row.api_key_enc.startswith("ENC:") and "sk-plain" not in row.api_key_enc
    loaded = await backend.call(PreferencesRepository, "load")
    assert loaded.providers["openai"].api_key == "sk-plain"


async def test_prefs_models_and_active_selection(backend):
    with pytest.raises(ValueError, match="name is required"):
        await backend.call(
            PreferencesRepository,
            "upsert_model",
            "dashscope",
            "my-model",
            source="extra",
        )
    await backend.call(
        PreferencesRepository,
        "upsert_model",
        "dashscope",
        "my-model",
        source="extra",
        name="My Model",
        context_size=32000,
    )
    loaded = await backend.call(PreferencesRepository, "load")
    assert loaded.models[("dashscope", "my-model")].name == "My Model"
    with pytest.raises(ValueError, match="provider is not configured"):
        await backend.call(
            PreferencesRepository,
            "set_active_models",
            default_provider_id="dashscope",
            default_model_id="my-model",
        )
    await backend.call(
        PreferencesRepository, "upsert_provider", "dashscope", {"api_key": "sk-k"}
    )
    active = await backend.call(
        PreferencesRepository,
        "set_active_models",
        default_provider_id="dashscope",
        default_model_id="my-model",
    )
    assert active["default_model_id"] == "my-model"
    loaded = await backend.call(PreferencesRepository, "load")
    resolved = loaded.resolve().default
    assert resolved.model_id == "my-model" and resolved.name == "My Model"
    assert resolved.chat_model == "DashScopeChatModel"
    await backend.call(PreferencesRepository, "delete_model", "dashscope", "my-model")
    with pytest.raises(LookupError):
        await backend.call(
            PreferencesRepository, "delete_model", "dashscope", "my-model"
        )


def _job_body(name="每日晨报", **overrides):
    return CronJobWrite.model_validate(
        {
            "name": name,
            "message": "生成日报",
            "datasource_id": "ds1",
            "schedule": {"type": "cron", "cron": "0 8 * * 1"},
            **overrides,
        }
    )


async def test_cron_crud_roundtrip(backend):
    job = await backend.call(CronJobRepository, "create", _job_body())
    assert job["enabled"] and job["schedule"]["cron"] == "0 8 * * mon"
    assert (await backend.call(CronJobRepository, "get", job["id"]))[
        "name"
    ] == "每日晨报"
    replaced = await backend.call(
        CronJobRepository, "replace", job["id"], _job_body(name="改名", enabled=False)
    )
    assert replaced["name"] == "改名" and not replaced["enabled"]
    assert (await backend.call(CronJobRepository, "set_enabled", job["id"], True))[
        "enabled"
    ]
    assert [j["id"] for j in await backend.call(CronJobRepository, "list")] == [
        job["id"]
    ]
    for field in ["user_id", "tenant_id", "workspace_id"]:
        other = replace(TEST_IDENTITY, **{field: "other"})
        with pytest.raises(LookupError):
            await backend.call(CronJobRepository, "get", job["id"], identity=other)
        assert await backend.call(CronJobRepository, "list", identity=other) == []
    async with backend.factory() as db:
        row = await db.get(CronJobRow, job["id"])
        row.next_run_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with backend.factory() as db:
        ready = await CronJobRepository.list_ready_to_run(db)
        assert [item["id"] for item in ready] == [job["id"]]
    await backend.call(CronJobRepository, "delete", job["id"])
    with pytest.raises(LookupError):
        await backend.call(CronJobRepository, "get", job["id"])
    async with backend.factory() as db:
        assert await CronJobRepository.list_ready_to_run(db) == []


_CARD_FIELDS = {
    "metric_name": "GMV",
    "caliber": "支付金额",
    "domain": "交易",
    "table": "orders",
    "formula_sql": "SELECT SUM(amount)",
}


async def _add_card(backend, chat, **overrides):
    return await backend.call(
        SettlementRepository,
        "add",
        **{
            "session_id": chat.session_id,
            "source_chat_id": chat.id,
            "type": "metric_caliber",
            "fields": dict(_CARD_FIELDS),
            **overrides,
        },
    )


async def test_settlement_add_and_list_desc(backend):
    chat = await _add_chat(backend)
    first = await _add_card(backend, chat)
    second = await _add_card(
        backend, chat, type="dimension_def", fields={"dimension_name": "渠道"}
    )
    cards = await backend.call(SettlementRepository, "list_by_session", chat.session_id)
    assert [c["id"] for c in cards] == [second["id"], first["id"]]
    assert cards[0]["fields"] == {"dimension_name": "渠道"}
    assert cards[1]["status"] == "pending" and cards[1]["created_at"] is not None
    assert (
        len(
            await backend.call(
                SettlementRepository,
                "list_by_session",
                chat.session_id,
                status="pending",
            )
        )
        == 2
    )
    assert (
        await backend.call(
            SettlementRepository, "list_by_session", chat.session_id, status="confirmed"
        )
        == []
    )
    assert await backend.call(SettlementRepository, "list_by_session", "other") == []


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "workspace_id"])
async def test_settlement_get_scoping(backend, field):
    chat = await _add_chat(backend)
    card = await _add_card(backend, chat)
    loaded = await backend.call(
        SettlementRepository, "get", card["id"], session_id=chat.session_id
    )
    assert loaded["id"] == card["id"]
    other = replace(TEST_IDENTITY, **{field: "other"})
    with pytest.raises(LookupError):
        await backend.call(
            SettlementRepository,
            "get",
            card["id"],
            session_id=chat.session_id,
            identity=other,
        )
    assert (
        await backend.call(
            SettlementRepository, "list_by_session", chat.session_id, identity=other
        )
        == []
    )
    for cid, sid in [(card["id"], "other"), ("missing", chat.session_id)]:
        with pytest.raises(LookupError):
            await backend.call(SettlementRepository, "get", cid, session_id=sid)


async def test_settlement_mark_queried_only_pending(backend):
    chat = await _add_chat(backend)
    pending, confirmed = await _add_card(backend, chat), await _add_card(backend, chat)
    await backend.call(
        SettlementRepository, "confirm", confirmed["id"], session_id=chat.session_id
    )
    await backend.call(
        SettlementRepository,
        "mark_queried",
        chat.session_id,
        [pending["id"], confirmed["id"], "missing"],
    )
    for card, expected in [(pending, "queried"), (confirmed, "confirmed")]:
        assert (
            await backend.call(
                SettlementRepository, "get", card["id"], session_id=chat.session_id
            )
        )["status"] == expected


async def test_settlement_confirm_and_dismiss(backend):
    chat = await _add_chat(backend)
    card = await _add_card(backend, chat)
    fields = {**_CARD_FIELDS, "caliber": "含退款"}
    confirmed = await backend.call(
        SettlementRepository,
        "confirm",
        card["id"],
        session_id=chat.session_id,
        fields=fields,
    )
    assert confirmed["status"] == "confirmed" and confirmed["fields"] == fields
    assert confirmed["confirmed_at"] is not None
    for operation in ["dismiss", "confirm"]:
        with pytest.raises(ValueError):
            await backend.call(
                SettlementRepository, operation, card["id"], session_id=chat.session_id
            )
    other = await _add_card(backend, chat)
    await backend.call(
        SettlementRepository, "mark_queried", chat.session_id, [other["id"]]
    )
    dismissed = await backend.call(
        SettlementRepository, "dismiss", other["id"], session_id=chat.session_id
    )
    assert dismissed["status"] == "dismissed"


async def test_settlement_delete_if_unconfirmed(backend):
    chat = await _add_chat(backend)
    card = await _add_card(backend, chat)
    for field in ["user_id", "tenant_id", "workspace_id"]:
        other = replace(TEST_IDENTITY, **{field: "other"})
        assert not await backend.call(
            SettlementRepository,
            "delete_if_unconfirmed",
            card["id"],
            session_id=chat.session_id,
            identity=other,
        )
    assert not await backend.call(
        SettlementRepository, "delete_if_unconfirmed", card["id"], session_id="other"
    )
    assert await backend.call(
        SettlementRepository,
        "delete_if_unconfirmed",
        card["id"],
        session_id=chat.session_id,
    )
    assert not await backend.call(
        SettlementRepository,
        "delete_if_unconfirmed",
        card["id"],
        session_id=chat.session_id,
    )
    confirmed = await _add_card(backend, chat)
    await backend.call(
        SettlementRepository, "confirm", confirmed["id"], session_id=chat.session_id
    )
    assert not await backend.call(
        SettlementRepository,
        "delete_if_unconfirmed",
        confirmed["id"],
        session_id=chat.session_id,
    )


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "workspace_id"])
async def test_session_owner_filter_precedes_pagination(backend, field):
    other = replace(TEST_IDENTITY, **{field: "other"})
    own_ids = set()
    for index, identity in enumerate([TEST_IDENTITY, other, TEST_IDENTITY, other]):
        session = await _add_session(backend, identity=identity, title=f"Item {index}")
        if identity == TEST_IDENTITY:
            own_ids.add(session.id)
    rows, total = await backend.call(SessionRepository, "list", page=1, page_size=1)
    second, total_again = await backend.call(
        SessionRepository, "list", page=2, page_size=1
    )
    assert total == total_again == 2
    assert len(rows) == len(second) == 1
    assert {rows[0][0].id, second[0][0].id} == own_ids
    unknown = replace(TEST_IDENTITY, user_id="unknown")
    assert await backend.call(SessionRepository, "list", identity=unknown) == ([], 0)
