"""Rollback eligibility must notice dependencies without deleting any data."""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.rollback_check import inspect_saved_rollback

from .test_legacy_recovery_bundle import KEY, context as bundle_context, save
from .test_legacy_import_coordinator import apply
from .test_legacy_target_validation import mutate


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX publication evidence")


@pytest.fixture
def context(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    save(result)
    apply(result)
    return result


def inspect_rollback(context, **kwargs):
    return inspect_saved_rollback(
        context[3],
        database_url=context[1],
        target_home=context[0].target_home,
        key=KEY,
        target_stopped=True,
        **kwargs,
    )


def add(context, table, values):
    identity = context[0].identities[0]
    columns = Base.metadata.tables[table].c
    values = {
        **{
            k: getattr(identity, k)
            for k in ("user_id", "tenant_id", "workspace_id")
            if k in columns
        },
        **values,
    }
    engine = create_engine(context[1])
    with engine.begin() as conn:
        conn.execute(insert(Base.metadata.tables[table]), values)
    engine.dispose()


def test_clean_import_is_eligible_with_no_database_or_file_changes(context):
    database = Path(context[1].removeprefix("sqlite:///"))
    before = database.read_bytes()
    paths = [context[0].target_home / f.destination for f in context[0].files]
    files = [(p.stat().st_ino, p.read_bytes(), p.stat().st_nlink) for p in paths]
    result = inspect_rollback(context)
    assert result.findings == () and result.rows == len(context[0].rows)
    assert result.report()["native_dependencies_clear"]
    assert not result.report()["rollback_available"]
    assert not result.report()["external_dependencies_checked"]
    assert inspect_rollback(context) == result
    assert database.read_bytes() == before
    assert [
        (p.stat().st_ino, p.read_bytes(), p.stat().st_nlink) for p in paths
    ] == files


@pytest.mark.parametrize(
    "table,values,field",
    [
        (
            "chat_commands",
            dict(id="new-command", chat_id="c1", kind="steer"),
            "chat_id",
        ),
        (
            "behavior_events",
            dict(id="new-behavior", session_id="s1", kind="artifact_access"),
            "session_id",
        ),
        (
            "score_deliveries",
            dict(
                id="new-score", chat_id="c1", name="feedback", data_type="CATEGORICAL"
            ),
            "chat_id",
        ),
        ("session_agent_states", dict(session_id="s1", state_json={}), "session_id"),
        (
            "channel_bindings",
            dict(channel="cli", external_key="new-binding", active_session_id="s1"),
            "active_session_id",
        ),
        (
            "skill_revisions",
            dict(
                id="new-revision",
                agent_id="default",
                skill_name="new-skill",
                revision_no=1,
                origin="create",
                source_ref="s1",
                files=[],
            ),
            "source_ref",
        ),
        (
            "sessions",
            dict(id="new-session", parent_session_id="s1"),
            "parent_session_id",
        ),
        (
            "sessions",
            dict(id="new-session", forked_from_chat_id="c1"),
            "forked_from_chat_id",
        ),
        (
            "chat_commands",
            dict(
                id="cross-user-command",
                user_id="another-user",
                chat_id="c1",
                kind="steer",
            ),
            "chat_id",
        ),
    ],
)
def test_remaining_native_references_block_rollback_without_exposing_rows(
    context, table, values, field
):
    add(context, table, values)
    result = inspect_rollback(context)
    assert any(
        f.code == "new_native_reference" and f.table == table and f.field == field
        for f in result.findings
    )
    assert not result.report()["native_dependencies_clear"]
    assert "new-command" not in str(result.report())
    assert apply(context).status == "already_committed"


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": [{"attachment_id": "a1"}]},
        {"c1": "dynamic-key-reference"},
        {"path": "artifacts/s1/report.txt"},
        {"model": {"provider_id": "openai", "model_id": "fixture/org:model"}},
    ],
)
def test_nested_json_candidates_require_review(context, payload):
    add(context, "channels", dict(config_json=payload))
    result = inspect_rollback(context)
    assert any(
        f.code == "possible_json_reference" and f.table == "channels"
        for f in result.findings
    )


def test_remaining_custom_model_using_imported_provider_blocks_rollback(context):
    add(
        context,
        "user_provider_models",
        dict(provider_id="openai", model_id="new-model", source="extra"),
    )
    result = inspect_rollback(context)
    assert any(f.code == "remaining_model_dependency" for f in result.findings)


def test_unrelated_existing_data_is_preserved_and_does_not_blanket_block(context):
    add(context, "sessions", dict(id="unrelated-session", user_id="another-user"))
    add(context, "channels", dict(config_json={"display": "unrelated content"}))
    assert inspect_rollback(context).findings == ()


@pytest.mark.parametrize(
    "statement,match",
    [
        ("CREATE TABLE custom_extension (id TEXT PRIMARY KEY)", "unrecognized"),
        ("ALTER TABLE chats ADD COLUMN custom_reference TEXT", "additional"),
        (
            "CREATE TRIGGER custom_trigger AFTER DELETE ON chats BEGIN SELECT 1; END",
            "triggers",
        ),
    ],
)
def test_unknown_schema_or_trigger_requires_review(context, statement, match):
    mutate(context[1], statement)
    with pytest.raises(MigrationError, match=match):
        inspect_rollback(context)


@pytest.mark.parametrize(
    "limits", [dict(max_rows=1), dict(max_json_bytes=1), dict(max_total_json_bytes=1)]
)
def test_scan_budgets_fail_closed_without_unbounded_json_transfer(context, limits):
    add(context, "channels", dict(config_json={"unrelated": "value"}))
    with pytest.raises(MigrationError, match="limit"):
        inspect_rollback(context, **limits)


def test_auth_schema_extensions_also_require_review(context):
    mutate(context[1], "ALTER TABLE users ADD COLUMN custom_session_ref TEXT")
    with pytest.raises(MigrationError, match="Auth-table"):
        inspect_rollback(context)


def test_added_cascade_cannot_silently_expand_a_future_delete(context):
    from sqlalchemy import ForeignKeyConstraint, MetaData

    metadata = MetaData()
    Base.metadata.tables["chats"].to_metadata(metadata)
    commands = Base.metadata.tables["chat_commands"].to_metadata(metadata)
    commands.append_constraint(
        ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE")
    )
    engine = create_engine(context[1])
    with engine.begin() as conn:
        Base.metadata.tables["chat_commands"].drop(conn)
        commands.create(conn)
    engine.dispose()
    with pytest.raises(MigrationError, match="foreign keys"):
        inspect_rollback(context)


@pytest.mark.parametrize("change", ["row", "file", "backup", "database_token"])
def test_changed_import_evidence_refuses_inspection(context, change):
    if change == "row":
        mutate(context[1], "UPDATE sessions SET title='user change'")
    elif change == "file":
        (context[0].target_home / context[0].files[0].destination).write_bytes(
            b"user change"
        )
    elif change == "backup":
        (
            context[0].source.home.parent / "backup" / context[0].files[0].source
        ).write_bytes(b"changed backup")
    else:
        mutate(context[1], "DELETE FROM legacy_import_targets")
    with pytest.raises(MigrationError):
        inspect_rollback(context)


def test_stopped_writers_and_positive_budgets_are_required(context):
    with pytest.raises(MigrationError, match="stopped"):
        inspect_saved_rollback(
            context[3],
            database_url=context[1],
            target_home=context[0].target_home,
            key=KEY,
        )
    with pytest.raises(MigrationError, match="positive"):
        inspect_rollback(context, max_rows=True)
