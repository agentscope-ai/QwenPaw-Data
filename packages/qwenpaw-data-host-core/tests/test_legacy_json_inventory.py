"""Offline migration uses synthetic old homes, never live user data."""

from __future__ import annotations

import json
from io import BytesIO
import os
from pathlib import Path

import pytest

from qwenpaw_data.host.core.migration import legacy_json as migration


USERS = {
    "old-user": {
        "user_id": "new-user",
        "tenant_id": "tenant",
        "workspace_id": "workspace",
    }
}
SECRET = "fixture-provider-secret-do-not-print"


def write(root: Path, relative: str, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def legacy_home(tmp_path):
    home = tmp_path / "old home"
    identity = {"user_id": "old-user", "attrs": {}}
    write(
        home,
        "host/sessions/s1.json",
        {"id": "s1", "identity": identity, "agent_id": "default", "chat_count": 1},
    )
    write(
        home,
        "host/chats/c1/chat.json",
        {
            "id": "c1",
            "session_id": "s1",
            "identity": identity,
            "sequence": 1,
            "last_sequence_number": 0,
            "attachments": [{"attachment_id": "a1"}],
        },
    )
    write(
        home,
        "host/chats/c1/events.jsonl",
        {
            "object": "message",
            "chat_id": "c1",
            "session_id": "s1",
            "sequence_number": 0,
            "content": "private chat body",
        },
    )
    write(
        home,
        "host/preferences/old-user.json",
        {"providers": {"openai": {"api_key_enc": SECRET}}, "models": {}},
    )
    write(
        home,
        "host/attachments/a1.json",
        {
            "id": "a1",
            "session_id": "s1",
            "identity": identity,
            "filename": "data.csv",
            "storage_path": "uploads/s1/data.csv",
        },
    )
    write(
        home,
        "host/cron/job1.json",
        {"id": "job1", "user_id": "old-user", "session_id": "s1"},
    )
    write(
        home,
        "host/settlement/card1.json",
        {
            "id": "card1",
            "user_id": "old-user",
            "session_id": "s1",
            "source_chat_id": "c1",
        },
    )
    write(
        home,
        "host/feedback/c1.json",
        [
            {
                "id": "feedback1",
                "user_id": "old-user",
                "session_id": "s1",
                "chat_id": "c1",
            }
        ],
    )
    write(home, "host/workspace/uploads/s1/data.csv", "attachment bytes")
    write(home, "host/workspace/artifacts/s1/report.html", "private report")
    (home / "host/workspace/artifacts/s1/empty").mkdir()
    return home


def codes(home, **kwargs):
    return {
        f.code for f in migration.inspect_source(home, users=USERS, **kwargs).findings
    }


def test_inventory_is_read_only_and_reports_no_private_payload(legacy_home):
    before = migration.scan_source(legacy_home)
    result = migration.inspect_source(legacy_home, users=USERS)
    assert result.findings == []
    report = result.report()
    assert report["stage"] == "inspection_only" and report["sql_written"] is False
    assert report["required_user_ids"] == ["old-user"]
    assert set(report["file_kinds"]) == {
        "sessions",
        "chats",
        "events",
        "preferences",
        "attachments",
        "cron",
        "settlement",
        "feedback",
        "uploads",
        "artifacts",
    }
    assert SECRET not in json.dumps(report) + repr(result)
    assert "private chat body" not in json.dumps(report) + repr(result)
    assert migration.scan_source(legacy_home).manifest() == before.manifest()
    assert not list(legacy_home.rglob("*.sqlite*"))
    assert not (legacy_home / ".auth_secret").exists()


def test_no_implicit_identity_or_default_tenant(legacy_home):
    result = migration.inspect_source(legacy_home)
    assert {f.code for f in result.findings} == {"identity_mapping_required"}
    assert result.required_user_ids == {"old-user"}


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "/absolute",
        "a/b",
        "a\\b",
        ".",
        "..",
        "NUL",
        "a:",
        "bad\x00id",
        "trailing.",
        " trailing",
    ],
)
def test_mapping_rejects_unsafe_target_paths(value):
    with pytest.raises(migration.MigrationError, match="Unsafe"):
        migration.identity_mapping(
            {"version": 1, "users": {"old": {**USERS["old-user"], "tenant_id": value}}}
        )


def test_mapping_never_merges_global_provider_credentials():
    with pytest.raises(migration.MigrationError, match="same target user_id"):
        migration.identity_mapping(
            {
                "version": 1,
                "users": {
                    "a": USERS["old-user"],
                    "b": {**USERS["old-user"], "workspace_id": "other"},
                },
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1,"version":1,"users":{}}',
        b'{"version":NaN,"users":{}}',
        b'"secret-private-invalid-input"',
    ],
)
def test_mapping_invalid_json_is_redacted(tmp_path, payload):
    path = tmp_path / "mapping.json"
    path.write_bytes(payload)
    with pytest.raises(migration.MigrationError) as exc:
        migration.load_identity_mapping(path)
    assert "secret-private-invalid-input" not in str(exc.value)


@pytest.mark.parametrize(
    "relative,patch,expected",
    [
        (
            "host/chats/c1/chat.json",
            {"session_id": "missing"},
            "missing_or_cross_owner_session",
        ),
        (
            "host/chats/c1/chat.json",
            {"identity": {"user_id": "other"}},
            "missing_or_cross_owner_session",
        ),
        (
            "host/chats/c1/chat.json",
            {"attachments": [{"attachment_id": "missing"}]},
            "missing_or_cross_session_attachment",
        ),
        (
            "host/attachments/a1.json",
            {"storage_path": "../../secret"},
            "unsafe_attachment_storage_path",
        ),
        (
            "host/chats/c1/events.jsonl",
            {"sequence_number": 4},
            "invalid_event_identity_or_sequence",
        ),
        (
            "host/chats/c1/events.jsonl",
            {"chat_id": "other"},
            "invalid_event_identity_or_sequence",
        ),
        (
            "host/chats/c1/chat.json",
            {"last_sequence_number": 5},
            "event_watermark_ahead_of_log",
        ),
        (
            "host/settlement/card1.json",
            {"source_chat_id": "missing"},
            "missing_or_cross_session_chat",
        ),
        (
            "host/sessions/s1.json",
            {"parent_session_id": "missing"},
            "missing_or_cross_owner_parent_session",
        ),
        (
            "host/sessions/s1.json",
            {"chat_count": 0},
            "session_chat_count_requires_reconciliation",
        ),
    ],
)
def test_broken_references_are_not_silently_accepted(
    legacy_home, relative, patch, expected
):
    path = legacy_home / relative
    value = json.loads(path.read_text())
    write(legacy_home, relative, {**value, **patch})
    assert expected in codes(legacy_home)


def test_non_session_state_requires_explicit_disposition(legacy_home):
    write(
        legacy_home,
        "host/workspace/sessions/console/local_s1.json",
        {"session_id": "s1"},
    )
    write(legacy_home, "host/workspace/sessions/dag/s1.json", {"nodes": []})
    write(legacy_home, "host/workspace/skills/custom/SKILL.md", "private skill")
    assert codes(legacy_home) == {
        "agent_state_mapping_required",
        "explicit_file_disposition_required",
    }


def test_backup_copies_every_byte_and_empty_directory_and_is_idempotent(
    legacy_home, tmp_path
):
    snapshot = migration.scan_source(legacy_home)
    dest = tmp_path / "backup"
    assert (
        migration.create_backup(snapshot, dest, source_stopped=True)
        == "created_and_verified"
    )
    assert (
        migration.create_backup(snapshot, dest, source_stopped=True)
        == "already_verified"
    )
    migration.verify_backup(dest, snapshot)
    assert (
        json.loads((dest / "migration-manifest.json").read_text())
        == snapshot.manifest()
    )
    assert (dest / "host/workspace/artifacts/s1/empty").is_dir()
    for record in snapshot.files:
        assert (dest / record.path).read_bytes() == (
            legacy_home / record.path
        ).read_bytes()
    if os.name != "nt":
        assert dest.stat().st_mode & 0o777 == 0o700
        assert all(
            (dest / f.path).stat().st_mode & 0o777 == 0o600 for f in snapshot.files
        )


def test_backup_requires_stopped_source_and_disjoint_destination(legacy_home, tmp_path):
    snapshot = migration.scan_source(legacy_home)
    with pytest.raises(migration.MigrationError, match="Stop all source"):
        migration.create_backup(snapshot, tmp_path / "backup")
    for destination in (legacy_home, legacy_home / "backup", legacy_home.parent):
        with pytest.raises(migration.MigrationError, match="disjoint"):
            migration.create_backup(snapshot, destination, source_stopped=True)


def test_existing_destination_is_never_overwritten(legacy_home, tmp_path):
    dest = tmp_path / "occupied"
    write(dest, "keep.json", {"value": "do not change"})
    with pytest.raises(migration.MigrationError):
        migration.create_backup(
            migration.scan_source(legacy_home), dest, source_stopped=True
        )
    assert json.loads((dest / "keep.json").read_text()) == {"value": "do not change"}
    assert not (dest / "host").exists()


def test_backup_tampering_is_detected_without_repair_or_overwrite(
    legacy_home, tmp_path
):
    snapshot = migration.scan_source(legacy_home)
    dest = tmp_path / "backup"
    migration.create_backup(snapshot, dest, source_stopped=True)
    path = dest / "host/chats/c1/chat.json"
    path.write_text("tampered")
    with pytest.raises(migration.MigrationError, match="verification"):
        migration.create_backup(snapshot, dest, source_stopped=True)
    assert path.read_text() == "tampered"


def test_source_changes_abort_before_creating_destination(legacy_home, tmp_path):
    snapshot = migration.scan_source(legacy_home)
    write(legacy_home, "host/chats/c1/chat.json", {"changed": True})
    with pytest.raises(migration.MigrationError, match="Source changed"):
        migration.create_backup(snapshot, tmp_path / "backup", source_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_partial_backup_is_retained_and_cannot_be_mistaken_for_complete(
    legacy_home, tmp_path, monkeypatch
):
    snapshot = migration.scan_source(legacy_home)
    original = migration._read_file

    def interrupted(home, path, expected):
        if (tmp_path / "backup/.incomplete").exists() and path.endswith("chat.json"):
            raise OSError("fixture disk failure")
        return original(home, path, expected)

    monkeypatch.setattr(migration, "_read_file", interrupted)
    with pytest.raises(migration.MigrationError, match="partial data retained"):
        migration.create_backup(snapshot, tmp_path / "backup", source_stopped=True)
    assert (tmp_path / "backup/.incomplete").is_file()
    assert not (tmp_path / "backup/migration-manifest.json").exists()
    monkeypatch.setattr(migration, "_read_file", original)
    with pytest.raises(migration.MigrationError, match="incomplete"):
        migration.create_backup(snapshot, tmp_path / "backup", source_stopped=True)


def test_symlinks_and_hardlinks_are_rejected_without_following(legacy_home, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET)
    link = legacy_home / "host/link"
    link.symlink_to(outside)
    with pytest.raises(migration.MigrationError, match="Symbolic links"):
        migration.scan_source(legacy_home)
    link.unlink()
    os.link(outside, link)
    with pytest.raises(migration.MigrationError, match="non-linked"):
        migration.scan_source(legacy_home)
    assert outside.read_text() == SECRET


def test_inventory_and_metadata_limits_fail_closed(legacy_home):
    for limits in (migration.Limits(files=1), migration.Limits(bytes=1)):
        with pytest.raises(migration.MigrationError, match="limits"):
            migration.scan_source(legacy_home, limits=limits)
    with pytest.raises(migration.MigrationError, match="Metadata exceeds"):
        migration.inspect_source(
            legacy_home, users=USERS, limits=migration.Limits(metadata_bytes=1)
        )


@pytest.mark.parametrize("value", [0, -1, True, float("inf")])
def test_inventory_limits_require_positive_integers(value):
    with pytest.raises(migration.MigrationError, match="positive"):
        migration.Limits(files=value)


def test_unreadable_directory_is_not_silently_omitted(legacy_home, monkeypatch):
    def walk(_root, *, followlinks, onerror):
        onerror(PermissionError("fixture unreadable directory"))
        yield  # pragma: no cover

    monkeypatch.setattr(migration.os, "walk", walk)
    with pytest.raises(migration.MigrationError, match="Cannot enumerate"):
        migration.scan_source(legacy_home)


@pytest.mark.parametrize("data,size", [(b"growing file", 1), (b"truncated", 100)])
def test_copying_cannot_exceed_or_truncate_inventory_size(data, size):
    with pytest.raises(migration.MigrationError, match="size changed"):
        list(migration._chunks(BytesIO(data), size))


def test_backup_retains_original_executable_mode_in_manifest(legacy_home, tmp_path):
    path = write(
        legacy_home, "host/workspace/skills/custom/script.py", "print('fixture')"
    )
    path.chmod(0o755)
    snapshot = migration.scan_source(legacy_home)
    source = next(f for f in snapshot.files if f.path.endswith("script.py"))
    assert source.source_mode == path.stat().st_mode & 0o7777
    destination = tmp_path / "backup"
    migration.create_backup(snapshot, destination, source_stopped=True)
    manifest = json.loads((destination / "migration-manifest.json").read_text())
    assert (
        next(f for f in manifest["files"] if f["path"] == source.path)["source_mode"]
        == source.source_mode
    )


@pytest.mark.parametrize(
    "version,supported",
    [
        ((3, 11, 9), False),
        ((3, 11, 10), True),
        ((3, 12, 3), False),
        ((3, 12, 4), True),
        ((3, 13, 0), True),
    ],
)
def test_windows_backup_rejects_python_without_private_mkdir(version, supported):
    if supported:
        migration._check_backup_platform("win32", version)
    else:
        with pytest.raises(migration.MigrationError, match="Private Windows backups"):
            migration._check_backup_platform("win32", version)
    migration._check_backup_platform("darwin", version)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX mode check; Windows uses a private DACL"
)
def test_existing_public_backup_is_rejected_not_chmodded(legacy_home, tmp_path):
    snapshot = migration.scan_source(legacy_home)
    dest = tmp_path / "backup"
    migration.create_backup(snapshot, dest, source_stopped=True)
    dest.chmod(0o755)
    with pytest.raises(migration.MigrationError, match="permissions"):
        migration.create_backup(snapshot, dest, source_stopped=True)
    assert dest.stat().st_mode & 0o777 == 0o755
