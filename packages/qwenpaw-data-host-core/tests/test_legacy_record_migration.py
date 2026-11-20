"""Pure row conversion plus real SQL persistence using synthetic legacy records."""

from copy import deepcopy
from datetime import datetime

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.records import convert_record


STAMP = "2026-10-01T12:34:56.123456+08:00"
USERS = {"old": {"user_id": "new", "tenant_id": "tenant", "workspace_id": "workspace"}}
OWNED = {"identity": {"user_id": "old", "attrs": {}}, "created_at": STAMP}
USER = {"user_id": "old", "created_at": STAMP}
DOCUMENTS = {
    "sessions": {
        **OWNED,
        "id": "s1",
        "agent_id": "default",
        "title": "private title",
        "datasource_id": "ds1",
        "channel": "console",
        "chat_count": 1,
        "updated_at": STAMP,
        "deleted_at": None,
        "parent_session_id": None,
        "forked_from_chat_id": None,
    },
    "chats": {
        **OWNED,
        "id": "c1",
        "session_id": "s1",
        "sequence": 1,
        "user_input": "private question",
        "datasource_id": "ds1",
        "dataset_names": ["sales"],
        "kind": "simple",
        "status": "completed",
        "last_sequence_number": -1,
        "started_at": STAMP,
        "completed_at": STAMP,
        "active_duration_ms": 45,
        "error": None,
        "plan": None,
        "artifact_comments": [
            {
                "path": "artifacts/s1/report.txt",
                "line_start": 1,
                "line_end": 2,
                "comment": "private comment",
            }
        ],
        "attachments": [{"attachment_id": "a1", "filename": "input.csv"}],
        "updated_at": STAMP,
    },
    "attachments": {
        **OWNED,
        "id": "a1",
        "session_id": "s1",
        "filename": "input.csv",
        "storage_path": "uploads/s1/input.csv",
    },
    "settlement": {
        **USER,
        "id": "card1",
        "session_id": "s1",
        "source_chat_id": "c1",
        "type": "insight",
        "fields": {"title": "private insight"},
        "status": "confirmed",
        "created_at": STAMP,
        "updated_at": STAMP,
        "confirmed_at": STAMP,
    },
    "feedback": {
        **USER,
        "id": "f1",
        "session_id": "s1",
        "chat_id": "c1",
        "kind": "artifact_comment",
        "reason": "private reason",
        "detail": "private detail",
        "artifact_ref": {
            "artifact_id": "artifact1",
            "content_hash": "hash1",
            "line_start": 1,
            "line_end": 2,
            "quote": "private quote",
        },
    },
    "cron": {
        **USER,
        "id": "job1",
        "name": "private schedule",
        "enabled": False,
        "message": "private task",
        "datasource_id": "ds1",
        "session_id": "s1",
        "schedule": {
            "type": "cron",
            "cron": "0 9 * * mon",
            "run_at": None,
            "timezone": "Asia/Shanghai",
        },
        "updated_at": STAMP,
    },
}


@pytest.mark.parametrize("kind", DOCUMENTS)
def test_convert_without_mutating_or_leaking_record(kind):
    original = deepcopy(DOCUMENTS[kind])
    result = convert_record(kind, original, users=USERS)
    assert original == DOCUMENTS[kind]
    assert result.values["user_id"] == "new"
    assert result.values["tenant_id"] == "tenant"
    assert result.values["workspace_id"] == "workspace"
    assert result.values["created_at"] == datetime.fromisoformat(STAMP)
    assert result.notices == ()
    assert "private" not in repr(result)


@pytest.mark.parametrize("kind", DOCUMENTS)
def test_unknown_fields_fail_instead_of_disappearing(kind):
    original = {
        **deepcopy(DOCUMENTS[kind]),
        "private-extra-field": "secret-not-to-print",
    }
    with pytest.raises(MigrationError) as exc:
        convert_record(kind, original, users=USERS)
    assert "private-extra-field" not in str(exc.value)
    assert "secret-not-to-print" not in str(exc.value)


@pytest.mark.parametrize("kind", DOCUMENTS)
@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-10-01T12:34:56", 123])
def test_aware_creation_time_is_required(kind, stamp):
    with pytest.raises(MigrationError):
        convert_record(kind, {**DOCUMENTS[kind], "created_at": stamp}, users=USERS)


@pytest.mark.parametrize("kind", DOCUMENTS)
def test_identity_is_always_explicit(kind):
    with pytest.raises(MigrationError, match="explicit identity"):
        convert_record(kind, DOCUMENTS[kind], users={})


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_count", True),
        ("chat_count", "1"),
        ("chat_count", -1),
        ("chat_count", 2**31),
        ("identity", {"user_id": "old", "attrs": {"bad": float("nan")}}),
        ("title", "\ud800"),
        ("id", "../outside"),
        ("agent_id", "CON"),
        ("updated_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_invalid_session_fields_are_not_coerced(field, value):
    with pytest.raises(MigrationError):
        convert_record("sessions", {**DOCUMENTS["sessions"], field: value}, users=USERS)


def test_legacy_identity_attrs_are_explicitly_accounted_for():
    doc = {
        **DOCUMENTS["sessions"],
        "identity": {"user_id": "old", "attrs": {"role": "admin"}},
    }
    result = convert_record("sessions", doc, users=USERS)
    assert result.notices == ("legacy_identity_attrs_retained_in_source_backup",)
    assert "role" not in result.values and "attrs" not in result.values
    assert result.values["share_scope"] == "none"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "running"),
        ("status", "created"),
        ("status", "made-up"),
        ("completed_at", None),
        ("completed_at", "2020-01-01T00:00:00Z"),
        ("sequence", False),
        ("last_sequence_number", -2),
        ("active_duration_ms", -1),
        ("active_duration_ms", 2**31),
        ("plan", {"nodes": [{"state": "failed"}]}),
        ("plan", {"tasks": [{"state": "completed"}]}),
        ("attachments", [{"attachment_id": "a1", "filename": "../a"}]),
        ("kind", "unknown"),
        ("error", {"code": "NEW_UNKNOWN_CODE", "message": "private error"}),
    ],
)
def test_nonrepresentable_chat_is_rejected_not_silently_changed(field, value):
    with pytest.raises(MigrationError) as exc:
        convert_record("chats", {**DOCUMENTS["chats"], field: value}, users=USERS)
    assert "private error" not in str(exc.value)


@pytest.mark.parametrize("plan", [None, {}, {"tasks": []}])
@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
def test_terminal_status_and_empty_plan_survive(plan, status):
    doc = {**DOCUMENTS["chats"], "plan": plan, "status": status}
    values = convert_record("chats", doc, users=USERS).values
    assert values["status"] == status
    assert values["plan"] == {"tasks": []}
    assert values["attachments_json"] == doc["attachments"]
    assert values["artifact_comments_json"] == doc["artifact_comments"]
    assert values["dataset_names"] == doc["dataset_names"]


def test_attachment_path_must_match_its_scoped_layout():
    with pytest.raises(MigrationError, match="storage path"):
        convert_record(
            "attachments",
            {**DOCUMENTS["attachments"], "storage_path": "uploads/other/input.csv"},
            users=USERS,
        )


def test_cron_is_not_enabled_or_scheduled_during_conversion():
    doc = {**DOCUMENTS["cron"], "enabled": True}
    with pytest.raises(MigrationError, match="disable-on-import consent"):
        convert_record("cron", doc, users=USERS)
    result = convert_record("cron", doc, users=USERS, disable_cron=True)
    assert result.notices == ("cron_disabled_on_import",)
    assert result.values["enabled"] is False
    assert result.values["next_run_at"] is None
    assert result.values["running_at"] is None
    assert doc["enabled"] is True
    with pytest.raises(MigrationError, match="boolean"):
        convert_record("cron", doc, users=USERS, disable_cron="yes")


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "cron", "cron": "0 9 * * mon", "run_at": STAMP},
        {"type": "once", "run_at": STAMP, "cron": "0 9 * * mon"},
        {"type": "once"},
        {"cron": "123 9 * * mon"},
        {"cron": "0 9 * * mon", "timezone": "NoSuchZone/private"},
        {"cron": "not cron"},
    ],
)
def test_contradictory_or_invalid_cron_schedule_is_not_repaired(schedule):
    with pytest.raises(MigrationError):
        convert_record("cron", {**DOCUMENTS["cron"], "schedule": schedule}, users=USERS)


def test_sunday_number_retains_legacy_api_meaning_without_scheduling():
    result = convert_record(
        "cron", {**DOCUMENTS["cron"], "schedule": {"cron": "0 9 * * 0"}}, users=USERS
    )
    assert result.values["schedule_json"]["cron"] == "0 9 * * sun"
    assert result.notices == ("cron_schedule_normalized",)
    assert result.values["next_run_at"] is None


async def test_all_converted_row_types_persist_and_reload_via_current_schema(tmp_path):
    from qwenpaw_data.host.core.db.tables import Base
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prepared = [
        convert_record(kind, doc, users=USERS) for kind, doc in DOCUMENTS.items()
    ]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory.begin() as db:
            for record in prepared:
                await db.execute(
                    insert(Base.metadata.tables[record.table]).values(**record.values)
                )
        async with factory() as db:
            for record in prepared:
                row = (
                    (await db.execute(select(Base.metadata.tables[record.table])))
                    .mappings()
                    .one()
                )
                assert {key: row[key] for key in record.values} == record.values
            identity = Identity(**USERS["old"])
            session = await SessionRepository(db, identity).get("s1")
            chat = await ChatRepository(db, identity).get("c1")
            assert session.title == DOCUMENTS["sessions"]["title"]
            assert session.identity == chat.identity == identity
            assert chat.user_input == DOCUMENTS["chats"]["user_input"]
            assert chat.plan.tasks == [] and chat.status == "completed"
            assert chat.attachments == DOCUMENTS["chats"]["attachments"]
    finally:
        await engine.dispose()
