"""Historical event bytes and UI projections must both survive SQL conversion."""

from copy import deepcopy

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from qwenpaw_data.host.core.migration.events import convert_events
from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.records import convert_record

from .test_legacy_record_migration import DOCUMENTS, STAMP, USERS


def event_documents():
    payloads = [
        {"object": "response", "id": "response1", "status": "created"},
        {
            "object": "message",
            "id": "m1",
            "sequence": 0,
            "type": "text",
            "role": "assistant",
            "content": [{"type": "text", "text": "private answer"}],
        },
        {
            "object": "segment",
            "segment": {
                "segment_id": "seg1",
                "title": "private title",
                "coverage": {"start_seq": 0, "end_seq": 1},
                "conclusion": "initial",
            },
        },
        {
            "object": "biz_event",
            "biz_event": {
                "event_id": "biz1",
                "seq": 1,
                "presentation": {
                    "card_type": "text",
                    "caption": "private caption",
                    "body": "initial",
                },
            },
        },
        {
            "object": "artifact.registered",
            "artifact": {
                "id": "artifact1",
                "session_id": "s1",
                "name": "report",
                "path": "artifacts/s1/report.txt",
                "created_at": STAMP,
                "updated_at": STAMP,
            },
        },
        {
            "object": "followup.generated",
            "followup": {"questions": ["initial question"]},
        },
        {
            "object": "segment",
            "segment": {
                "segment_id": "seg1",
                "title": "private title",
                "coverage": {"start_seq": 0, "end_seq": 1},
                "conclusion": "final conclusion",
            },
        },
        {
            "object": "biz_event",
            "biz_event": {
                "event_id": "biz1",
                "seq": 3,
                "presentation": {
                    "card_type": "text",
                    "caption": "private caption",
                    "body": "final body",
                },
            },
        },
        {"object": "followup.generated", "followup": {"questions": ["final question"]}},
        {"object": "response", "id": "response1", "status": "completed"},
    ]
    return [
        {
            **p,
            "session_id": "s1",
            "chat_id": "c1",
            "sequence_number": index,
            "created_at": STAMP,
        }
        for index, p in enumerate(payloads)
    ]


def prepare_chat(events, **overrides):
    return convert_record(
        "chats",
        {**DOCUMENTS["chats"], "last_sequence_number": len(events) - 1, **overrides},
        users=USERS,
    )


def test_raw_events_are_preserved_and_latest_projections_are_rebuilt():
    events = event_documents()
    original = deepcopy(events)
    rows = convert_events(events, chat=prepare_chat(events))
    history = [r for r in rows if r.table == "chat_events"]
    assert [r.values["payload"] for r in history] == original == events
    assert all("private" not in repr(row) for row in rows)
    projections = {r.table: r.values for r in rows if r.table != "chat_events"}
    assert set(projections) == {"segments", "biz_events", "artifacts", "followups"}
    assert projections["segments"]["conclusion"] == "final conclusion"
    assert projections["biz_events"]["presentation_json"]["body"] == "final body"
    assert projections["biz_events"]["seq"] == 1
    assert projections["followups"]["questions_json"] == ["final question"]
    assert projections["artifacts"]["chat_id"] == "c1"
    events[1]["content"][0]["text"] = "changed source object"
    assert history[1].values["payload"] == original[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence_number", False),
        ("sequence_number", 9),
        ("chat_id", "other"),
        ("session_id", "other"),
        ("created_at", "2026-10-01T12:34:56"),
        ("object", "task_status"),
        ("private-unknown", "private secret"),
    ],
)
def test_bad_events_are_rejected_without_repair_or_payload_disclosure(field, value):
    events = event_documents()
    events[0][field] = value
    with pytest.raises(MigrationError) as exc:
        convert_events(events, chat=prepare_chat(events))
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(
    "index,nested,field,value",
    [
        (2, "segment", "chat_id", "other"),
        (2, "segment", "coverage", {"start_seq": 0, "end_seq": 999}),
        (2, "segment", "coverage", {"start_seq": 2, "end_seq": 1}),
        (3, "biz_event", "chat_id", "other"),
        (3, "biz_event", "seq", -1),
        (3, "biz_event", "seq", 2**31),
        (4, "artifact", "session_id", "other"),
        (4, "artifact", "chat_id", "other"),
        (4, "artifact", "path", "../escape"),
        (4, "artifact", "path", "/tmp/absolute"),
        (4, "artifact", "path", "C:/drive"),
        (4, "artifact", "created_at", "2026-10-01T12:34:56"),
        (5, "followup", "chat_id", "other"),
    ],
)
def test_invalid_projection_ownership_and_paths_are_refused(
    index, nested, field, value
):
    events = event_documents()
    events[index][nested][field] = value
    with pytest.raises(MigrationError):
        convert_events(events, chat=prepare_chat(events))


@pytest.mark.parametrize("watermark", [0, 10])
def test_watermark_is_never_silently_repaired(watermark):
    events = event_documents()
    with pytest.raises(MigrationError, match="watermark"):
        convert_events(
            events, chat=prepare_chat(events, last_sequence_number=watermark)
        )


@pytest.mark.parametrize("events", [[], event_documents()[:-1]])
def test_missing_terminal_event_requires_reconciliation(events):
    with pytest.raises(MigrationError, match="terminal response"):
        convert_events(events, chat=prepare_chat(events))


def test_terminal_status_disagreement_and_post_terminal_response_are_rejected():
    events = event_documents()
    with pytest.raises(MigrationError, match="terminal response"):
        convert_events(events, chat=prepare_chat(events, status="failed"))
    events.append(
        {**events[-1], "sequence_number": len(events), "status": "in_progress"}
    )
    with pytest.raises(MigrationError, match="after its terminal"):
        convert_events(events, chat=prepare_chat(events))


def test_cancelled_wire_status_maps_to_canceled_chat_without_rewriting_payload():
    events = event_documents()
    events[-1]["status"] = "cancelled"
    rows = convert_events(events, chat=prepare_chat(events, status="canceled"))
    assert rows[len(events) - 1].values["payload"]["status"] == "cancelled"


async def test_current_snapshot_and_replay_read_converted_sql_rows(tmp_path):
    from qwenpaw_data.host.core.api.mappers import chat_bundle_to_schema
    from qwenpaw_data.host.core.db.tables import Base, ChatEventRow
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
    from qwenpaw_data.host.core.repositories.snapshot import SnapshotRepository

    events = event_documents()
    chat = prepare_chat(events)
    records = [
        convert_record("sessions", DOCUMENTS["sessions"], users=USERS),
        chat,
        *convert_events(events, chat=chat),
    ]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory.begin() as db:
            for record in records:
                await db.execute(
                    insert(Base.metadata.tables[record.table]).values(**record.values)
                )
        async with factory() as db:
            identity = Identity(**USERS["old"])
            _, active, bundles, more = await SnapshotRepository(db, identity).load("s1")
            assert not active and not more and len(bundles) == 1
            snapshot = chat_bundle_to_schema(bundles[0]).model_dump(mode="json")
            assert snapshot["segments"][0]["conclusion"] == "final conclusion"
            assert snapshot["biz_events"][0]["presentation"]["body"] == "final body"
            assert snapshot["artifacts"][0]["path"] == "artifacts/s1/report.txt"
            assert snapshot["followup"]["questions"] == ["final question"]
            replay = await ChatEventRepository(db, identity).read_after("c1", -1)
            assert len(replay) == len(events) and replay[-1].status == "completed"
            assert await ChatEventRepository(db, identity).terminal_delivered("c1", 9)
            assert not await ChatEventRepository(db, identity).terminal_delivered(
                "c1", 8
            )
            stored = list(
                await db.scalars(
                    select(ChatEventRow).order_by(ChatEventRow.sequence_number)
                )
            )
            assert [r.payload for r in stored] == events
    finally:
        await engine.dispose()
