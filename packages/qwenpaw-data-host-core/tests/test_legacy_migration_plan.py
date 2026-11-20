"""Whole-source preparation must not hide unsupported data or write a target."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from qwenpaw_data.host.core.migration.credentials import CredentialRewriter
from qwenpaw_data.host.core.migration.legacy_json import (
    MigrationError,
    create_backup,
    scan_source,
)
from qwenpaw_data.host.core.migration.plan import prepare_migration
from qwenpaw_data.host.core.providers import provider_registry

from .test_legacy_record_migration import DOCUMENTS, USERS
from .test_legacy_event_migration import event_documents
from .test_legacy_preferences_migration import DOCUMENT as PREFERENCES, CUSTOM


def write(home, path, document):
    file = home / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def source(tmp_path):
    home = tmp_path / "old"
    events = event_documents()
    for kind, original in DOCUMENTS.items():
        doc = deepcopy(original)
        if kind == "chats":
            doc["last_sequence_number"] = len(events) - 1
            path = "host/chats/c1/chat.json"
        elif kind == "feedback":
            doc, path = [doc], "host/feedback/c1.json"
        else:
            path = f"host/{kind}/{doc['id']}.json"
        write(home, path, doc)
    (home / "host/chats/c1/events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    write(home, "host/preferences/old.json", PREFERENCES)
    for relative, content in [
        ("uploads/s1/input.csv", "x,y\n1,2\n"),
        ("artifacts/s1/report.txt", "private report"),
    ]:
        file = home / "host/workspace" / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    (home / "host/workspace/artifacts/s1/empty").mkdir()
    return home


def prepare(source, **overrides):
    backup = source.parent / "backup"
    create_backup(scan_source(source), backup, source_stopped=True)
    kwargs = dict(
        backup=backup,
        target_home=source.parent / "new",
        users=USERS,
        credentials=CredentialRewriter(source_key=None, target_key=bytes(range(32))),
        registry=provider_registry,
        prepared_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
        target_context_trigger_ratio=0.8,
        target_context_reserve_ratio=0.1,
        model_context_sizes={"old": {CUSTOM: 77788}},
        source_stopped=True,
    )
    kwargs.update(overrides)
    return prepare_migration(source, **kwargs)


def test_complete_conversion_is_still_not_executable_or_target_writing(source):
    before = scan_source(source).digest
    plan = prepare(source)
    assert plan.issues == ()
    report = plan.report()
    assert report["conversion_complete"] is True
    assert report["import_ready"] is False
    assert report["target_database_checked"] is False
    assert report["target_identity_membership_checked"] is False
    assert report["sql_written"] is False and report["target_files_written"] is False
    assert report["proposed_files"] == 2
    assert report["prepared_rows"]["chat_events"] == 10
    assert report["prepared_rows"]["user_provider_models"] == 2
    assert len(report["prepared_rows"]) == 15
    assert scan_source(source).digest == before
    assert not plan.target_home.exists()
    assert "synthetic-private-key" not in json.dumps(report) + repr(plan)
    assert "private question" not in json.dumps(report) + repr(plan)
    again = prepare(source)
    assert again.report() == report
    assert again.files == plan.files and again.directories == plan.directories


def test_destinations_match_native_user_agent_paths_and_keep_empty_directories(source):
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.paths import UserAgentPaths

    plan = prepare(source)
    paths = UserAgentPaths.from_identity(
        home=plan.target_home,
        identity=Identity(**USERS["old"]),
        agent_id="default",
        session_id="s1",
    )
    for file in plan.files:
        relative = Path(file.source).relative_to("host/workspace")
        assert plan.target_home / file.destination == paths.workspace / relative
        assert file.size == (source / file.source).stat().st_size
    assert any(path.endswith("artifacts/s1/empty") for path in plan.directories)


@pytest.mark.parametrize(
    "relative",
    [
        "host/workspace/.mcp",
        "host/workspace/skills/custom/SKILL.md",
        "host/workspace/sessions/dag/old_s1.json",
        "host/workspace/sessions/console/old_s1.json",
        "host/unknown.json",
    ],
)
def test_unassigned_data_is_reported_not_silently_dropped(source, relative):
    write(source, relative, {"private-data": "must survive"})
    plan = prepare(source)
    assert not plan.report()["conversion_complete"]
    assert any(issue.path == relative for issue in plan.issues)
    assert (source.parent / "backup" / relative).read_bytes() == (
        source / relative
    ).read_bytes()
    assert "must survive" not in json.dumps(plan.report())


def test_unknown_record_field_is_a_plan_blocker_even_if_other_records_convert(source):
    doc = {**DOCUMENTS["sessions"], "unknown": "private-value"}
    write(source, "host/sessions/s1.json", doc)
    plan = prepare(source)
    assert any(i.code == "record_conversion_failed" for i in plan.issues)
    assert plan.report()["prepared_rows"]["chats"] == 1
    assert not plan.report()["conversion_complete"]
    assert "private-value" not in json.dumps(plan.report())


def test_missing_file_and_cross_scope_artifact_reference_block_complete_plan(source):
    doc = deepcopy(DOCUMENTS["feedback"])
    doc["artifact_ref"]["artifact_id"] = "not-present"
    write(source, "host/feedback/c1.json", [doc])
    (source / "host/workspace/artifacts/s1/report.txt").unlink()
    plan = prepare(source)
    codes = {i.code for i in plan.issues}
    assert "missing_or_unscoped_referenced_file" in codes
    assert "missing_or_unscoped_commented_file" in codes
    assert "missing_or_cross_scope_feedback_artifact" in codes


def test_duplicate_sql_keys_block_plan_without_overwriting_one_record(source):
    write(
        source, "host/feedback/c1.json", [DOCUMENTS["feedback"], DOCUMENTS["feedback"]]
    )
    plan = prepare(source)
    assert any(i.code == "duplicate_target_primary_key" for i in plan.issues)
    assert plan.report()["prepared_rows"]["feedbacks"] == 1


@pytest.mark.parametrize("entry", ["file", "symlink"])
def test_target_file_conflict_is_not_overwritten(source, entry):
    target = source.parent / "new"
    file = target / "tenant/workspace/new/default/workspace/uploads/s1/input.csv"
    file.parent.mkdir(parents=True)
    if entry == "file":
        file.write_text("existing target data")
    else:
        try:
            file.symlink_to(source / "host/workspace/uploads/s1/input.csv")
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable")
            raise
    plan = prepare(source)
    assert any(i.code == "file_placement_failed" for i in plan.issues)
    if entry == "file":
        assert file.read_text() == "existing target data"
    else:
        assert file.is_symlink()


def test_source_backup_and_target_are_disjoint(source):
    for target in (
        source,
        source / "inside",
        source.parent,
        source.parent / "backup/inside",
    ):
        with pytest.raises(MigrationError, match="disjoint"):
            prepare(source, target_home=target)
    with pytest.raises(MigrationError, match="disjoint"):
        prepare(source, backup=source)


def test_stopped_writers_and_verified_backup_are_required(source):
    with pytest.raises(MigrationError, match="stopped"):
        prepare(source, source_stopped=False)
    (source.parent / "backup/host/sessions/s1.json").write_text("tampered")
    # Bypass the fixture's backup verification to exercise the planner gate.
    from unittest.mock import patch

    with patch(__name__ + ".create_backup"):
        with pytest.raises(MigrationError, match="verification"):
            prepare(source)


def test_source_changes_during_conversion_invalidate_entire_plan(source, monkeypatch):
    from qwenpaw_data.host.core.migration import plan as module

    def changed(home, **kwargs):
        (source / "host/workspace/artifacts/s1/report.txt").write_text(
            "changed during planning"
        )
        return scan_source(home, **kwargs)

    monkeypatch.setattr(module, "scan_source", changed)
    with pytest.raises(MigrationError, match="Source changed"):
        prepare(source)


def test_unknown_context_size_user_is_not_ignored(source):
    with pytest.raises(MigrationError, match="without legacy preferences"):
        prepare(source, model_context_sizes={"typo-user": {CUSTOM: 77788}})


def test_enabled_cron_requires_explicit_planned_disabling(source):
    write(source, "host/cron/job1.json", {**DOCUMENTS["cron"], "enabled": True})
    assert any(i.code == "record_conversion_failed" for i in prepare(source).issues)
    approved = prepare(source, disable_cron=True)
    assert approved.issues == ()
    assert any(n.code == "cron_disabled_on_import" for n in approved.notices)
    assert not next(
        r.values["enabled"] for r in approved.rows if r.table == "cron_jobs"
    )


@pytest.mark.parametrize(
    "path,content,known",
    [
        ("host/sessions/.s1.json.lock", "", True),
        ("host/sessions/.s1.json.lock", "private material", False),
        ("host/unknown.lock", "", False),
    ],
)
def test_only_recognized_empty_lock_files_are_archived_without_blocking(
    source, path, content, known
):
    file = source / path
    file.write_text(content)
    plan = prepare(source)
    if known:
        assert plan.issues == ()
        assert any(
            n.code == "empty_lock_retained_in_backup" and n.path == path
            for n in plan.notices
        )
    else:
        assert any(
            i.code == "explicit_file_disposition_required" and i.path == path
            for i in plan.issues
        )


def test_target_case_collision_is_detected_even_for_directories(source):
    (source.parent / "new/TENANT").mkdir(parents=True)
    plan = prepare(source)
    assert any("case-colliding" in i.message for i in plan.issues)


def test_native_agent_scope_aliases_do_not_silently_merge(source):
    write(
        source,
        "host/sessions/s2.json",
        {**DOCUMENTS["sessions"], "id": "s2", "agent_id": "DEFAULT", "chat_count": 0},
    )
    write(source, "host/workspace/artifacts/s2/extra.json", {"private": "payload"})
    plan = prepare(source)
    assert any("collide" in i.message for i in plan.issues)


def test_known_cli_logs_are_explicitly_archived_not_copied_as_live_runtime_state(
    source,
):
    (source / "host/qwenpaw_data.log").write_text("historical private log")
    plan = prepare(source)
    assert plan.issues == ()
    assert any(n.code == "historical_cli_log_retained_in_backup" for n in plan.notices)
    assert (
        source.parent / "backup/host/qwenpaw_data.log"
    ).read_text() == "historical private log"
    assert not any(f.source.endswith("qwenpaw_data.log") for f in plan.files)
    assert "historical private log" not in json.dumps(plan.report())


def test_unicode_aliases_cannot_merge_future_empty_agent_workspaces(source):
    write(
        source,
        "host/sessions/s2.json",
        {**DOCUMENTS["sessions"], "id": "s2", "agent_id": "Café", "chat_count": 0},
    )
    write(
        source,
        "host/sessions/s3.json",
        {
            **DOCUMENTS["sessions"],
            "id": "s3",
            "agent_id": "Cafe\u0301",
            "chat_count": 0,
        },
    )
    plan = prepare(source)
    assert any(i.code == "workspace_placement_failed" for i in plan.issues)


def test_unicode_aliases_cannot_merge_target_user_preferences():
    from qwenpaw_data.host.core.migration.legacy_json import identity_mapping

    users = {
        "a": {"user_id": "José", "tenant_id": "t", "workspace_id": "w"},
        "b": {"user_id": "Jose\u0301", "tenant_id": "t", "workspace_id": "w"},
    }
    with pytest.raises(MigrationError, match="same target user_id"):
        identity_mapping({"version": 1, "users": users})
