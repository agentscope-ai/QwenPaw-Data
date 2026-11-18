from __future__ import annotations

import json
import os

import pytest

from qwenpaw_data.cli.main import main, build_parser


def legacy_source(tmp_path):
    home = tmp_path / "legacy home"
    sessions = home / "host/sessions"
    sessions.mkdir(parents=True)
    (sessions / "s1.json").write_text(
        json.dumps(
            {
                "id": "s1",
                "identity": {"user_id": "old-user"},
                "chat_count": 0,
                "title": "private title that must not appear in inventory",
            }
        )
    )
    return home


def test_discover_reports_old_sqlite_even_with_a_new_store(tmp_path, capsys):
    home = tmp_path / "old-home"
    args = ["migration", "discover", "--source-home", str(home)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == []
    assert not home.exists()
    old = home / "host/host.db"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"fixture - must not be opened as SQLite")
    target = home / "control_plane.db"
    target.touch()
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["default_database_exists"] and report["default_database"] == str(target)
    assert report["candidates"] == [{"layout": "oss-host-sqlite-v0.3", "candidate": str(old)}]
    assert old.read_bytes() == b"fixture - must not be opened as SQLite"


def test_cli_inventory_exit_status_and_explicit_mapping(tmp_path, capsys):
    source = legacy_source(tmp_path)
    args = ["migration", "inspect", "--source-home", str(source)]
    assert main(args) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["required_user_ids"] == ["old-user"]
    assert "private title" not in output
    mapping = tmp_path / "map.json"
    mapping.write_text(
        json.dumps(
            {
                "version": 1,
                "users": {
                    "old-user": {
                        "user_id": "target-user",
                        "tenant_id": "tenant",
                        "workspace_id": "workspace",
                    }
                },
            }
        )
    )
    assert main([*args, "--identity-map", str(mapping)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["findings"] == [] and report["sql_written"] is False


def test_cli_backup_is_explicit_and_idempotent(tmp_path, capsys):
    source = legacy_source(tmp_path)
    destination = tmp_path / "backup"
    args = [
        "migration",
        "backup",
        "--source-home",
        str(source),
        "--destination",
        str(destination),
        "--source-stopped",
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "created_and_verified"
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "already_verified" and report["sql_written"] is False
    assert "credentials" in report["warning"]


@pytest.mark.parametrize(
    "args",
    [
        ["migration"],
        ["migration", "inspect"],
        ["migration", "backup", "--source-home", "old", "--destination", "new"],
    ],
)
def test_cli_migration_has_no_implicit_source_or_writer_acknowledgment(args):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(args)
    assert exc.value.code == 2


def test_cli_rejects_corrupt_json_without_exposing_contents(tmp_path, capsys):
    source = legacy_source(tmp_path)
    (source / "host/sessions/s1.json").write_text('{"private-token-value":')
    assert main(["migration", "inspect", "--source-home", str(source)]) == 1
    output = capsys.readouterr()
    assert "private-token-value" not in output.err + output.out
    assert "Invalid JSON document" in output.err


def plan_args(tmp_path, monkeypatch):
    from qwenpaw_data.host.core.migration.legacy_json import create_backup, scan_source

    source = legacy_source(tmp_path)
    file = source / "host/sessions/s1.json"
    doc = json.loads(file.read_text())
    doc.update(created_at="2026-11-01T00:00:00Z", updated_at="2026-11-01T00:00:00Z")
    file.write_text(json.dumps(doc))
    backup = tmp_path / "backup"
    create_backup(scan_source(source), backup, source_stopped=True)
    mapping = tmp_path / "identities.json"
    mapping.write_text(
        json.dumps(
            {
                "version": 1,
                "users": {
                    "old-user": {
                        "user_id": "new-user",
                        "tenant_id": "tenant",
                        "workspace_id": "workspace",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("QPD_MIGRATION_TEST_KEY", "ab" * 32)
    return [
        "migration",
        "plan",
        "--source-home",
        str(source),
        "--backup",
        str(backup),
        "--target-home",
        str(tmp_path / "target"),
        "--identity-map",
        str(mapping),
        "--source-stopped",
        "--target-key-env",
        "QPD_MIGRATION_TEST_KEY",
        "--context-trigger-ratio",
        "0.8",
        "--context-reserve-ratio",
        "0.1",
    ]


def test_cli_plan_is_read_only_and_distinguishes_conversion_from_import(
    tmp_path, monkeypatch, capsys
):
    args = plan_args(tmp_path, monkeypatch)
    assert main(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["stage"] == "read_only_preparation"
    assert report["conversion_complete"] is True and report["import_ready"] is False
    assert report["prepared_rows"] == {"sessions": 1}
    assert report["target_database_checked"] is False
    assert not (tmp_path / "target").exists()
    assert "ab" * 32 not in output and "private title" not in output


def test_cli_plan_reports_unknown_data_without_target_writes(
    tmp_path, monkeypatch, capsys
):
    from qwenpaw_data.host.core.migration.legacy_json import create_backup, scan_source

    args = plan_args(tmp_path, monkeypatch)
    source = tmp_path / "legacy home"
    (source / "host/unknown.json").write_text('{"private": "do-not-disclose"}')
    fresh_backup = tmp_path / "fresh-backup"
    create_backup(scan_source(source), fresh_backup, source_stopped=True)
    args[args.index("--backup") + 1] = str(fresh_backup)
    assert main(args) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["conversion_complete"] is False
    assert report["issues"][0]["code"] == "explicit_file_disposition_required"
    assert "do-not-disclose" not in output and not (tmp_path / "target").exists()


@pytest.mark.parametrize("value", ["not-hex-secret", "aa", "", "ab" * 33])
def test_cli_plan_rejects_bad_keys_without_printing_them(
    tmp_path, monkeypatch, capsys, value
):
    args = plan_args(tmp_path, monkeypatch)
    monkeypatch.setenv("QPD_MIGRATION_TEST_KEY", value)
    assert main(args) == 1
    output = capsys.readouterr()
    assert "not-hex-secret" not in output.err + output.out
    assert "ab" * 33 not in output.err + output.out
    assert not (tmp_path / "target").exists()


def test_cli_plan_requires_explicit_keys_instead_of_ambient_runtime_secret(
    tmp_path, monkeypatch, capsys
):
    args = plan_args(tmp_path, monkeypatch)
    monkeypatch.delenv("QPD_MIGRATION_TEST_KEY")
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", "cd" * 32)
    assert main(args) == 1
    assert "master-key variable" in capsys.readouterr().err


@pytest.mark.parametrize(
    "remove",
    [
        "--source-stopped",
        "--backup",
        "--identity-map",
        "--target-home",
        "--target-key-env",
        "--context-trigger-ratio",
        "--context-reserve-ratio",
    ],
)
def test_cli_plan_requires_explicit_inputs(tmp_path, monkeypatch, remove):
    args = plan_args(tmp_path, monkeypatch)
    index = args.index(remove)
    del args[index : index + (1 if remove == "--source-stopped" else 2)]
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(args)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "document",
    [
        {"version": True, "users": {}},
        {"version": 2, "users": {}},
        {"version": 1, "users": {}, "unknown": "private"},
        {"version": 1, "users": []},
    ],
)
def test_cli_plan_rejects_unversioned_or_unknown_model_mapping(
    tmp_path, monkeypatch, capsys, document
):
    args = plan_args(tmp_path, monkeypatch)
    mapping = tmp_path / "models.json"
    mapping.write_text(json.dumps(document))
    assert main([*args, "--model-context-sizes", str(mapping)]) == 1
    assert "Model size mapping" in capsys.readouterr().err


def test_migration_inspection_does_not_create_cli_logs_in_source_home(
    tmp_path, monkeypatch, capsys
):
    from qwenpaw_data.host.core.migration.legacy_json import scan_source

    source = legacy_source(tmp_path)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(source))
    before = scan_source(source).digest
    assert main(["migration", "inspect", "--source-home", str(source)]) == 2
    capsys.readouterr()
    assert scan_source(source).digest == before
    assert not (source / "host/qwenpaw_data.log").exists()


def test_migration_error_suspends_and_restores_existing_cli_file_handler(
    tmp_path, monkeypatch, capsys
):
    import logging
    import importlib
    from qwenpaw_data.cli.logging_config import configure_cli_logging
    from qwenpaw_data.host.core.migration.legacy_json import scan_source

    source = legacy_source(tmp_path)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(source))
    logfile = configure_cli_logging()

    def dotenv_warning(*, override):
        logging.getLogger("dotenv").warning("migration-dotenv-warning")

    monkeypatch.setattr(
        importlib.import_module("qwenpaw_data.cli.main"),
        "load_qwenpaw_data_env",
        dotenv_warning,
    )
    (source / "host/sessions/s1.json").write_text('{"private-corrupt-input":')
    before = scan_source(source).digest
    assert main(["migration", "inspect", "--source-home", str(source)]) == 1
    capsys.readouterr()
    assert scan_source(source).digest == before
    logging.getLogger("migration-test").info("after-migration-marker")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "after-migration-marker" in logfile.read_text()
    assert "migration-dotenv-warning" not in logfile.read_text()


def test_cli_optional_target_check_reports_auth_blockers_without_bootstrapping(
    tmp_path, monkeypatch, capsys
):
    from sqlalchemy import create_engine
    from qwenpaw_data_auth.database import metadata
    from qwenpaw_data.host.core.db.tables import Base

    args = plan_args(tmp_path, monkeypatch)
    db = tmp_path / "explicit.sqlite"
    url = f"sqlite:///{db}"
    engine = create_engine(url)
    metadata.create_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    before = db.read_bytes()
    monkeypatch.setenv("QPD_MIGRATION_TEST_DB", url)
    assert (
        main([*args, "--target-db-env", "QPD_MIGRATION_TEST_DB", "--target-stopped"])
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert report["conversion_complete"] is True
    assert report["target_database_checked"] is True
    assert report["target_identity_membership_checked"] is True
    assert (
        report["target_validation"]["issues"][0]["code"]
        == "target_identity_not_authorized"
    )
    assert report["target_validation"]["target_checks_passed"] is False
    assert report["import_ready"] is False
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        ["--target-stopped"],
        ["--target-db-env", "QPD_MIGRATION_TEST_DB"],
        ["--target-db-env", "QPD_MIGRATION_TEST_DB", "--target-stopped"],
        [
            "--target-db-env",
            "postgresql://private-user:private-key@localhost/db",
            "--target-stopped",
        ],
    ],
)
def test_cli_target_selection_requires_explicit_name_value_and_stop_ack(
    tmp_path, monkeypatch, capsys, extra
):
    args = plan_args(tmp_path, monkeypatch)
    monkeypatch.delenv("QPD_MIGRATION_TEST_DB", raising=False)
    assert main([*args, *extra]) == 1
    output = capsys.readouterr()
    assert "private-key" not in output.out + output.err
    assert not (tmp_path / "target").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX local key provisioning")
def test_cli_local_key_setup_is_explicit_private_and_repeatable(
    tmp_path, monkeypatch, capsys
):
    from sqlalchemy import create_engine
    from qwenpaw_data.host.core.db.tables import Base

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    database = tmp_path / "existing.sqlite"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    before = database.read_bytes()
    monkeypatch.setenv("QPD_MIGRATION_TEST_DB", url)
    monkeypatch.setenv("QPD_MIGRATION_TEST_KEY", "ab" * 32)
    args = [
        "migration",
        "provision-key",
        "--target-home",
        str(target),
        "--target-key-env",
        "QPD_MIGRATION_TEST_KEY",
        "--target-db-env",
        "QPD_MIGRATION_TEST_DB",
        "--target-stopped",
    ]
    for expected in ("created_and_verified", "already_verified"):
        assert main(args) == 0
        output = capsys.readouterr()
        report = json.loads(output.out)
        assert report["status"] == expected
        assert not report["sql_written"] and not report["import_ready"]
        assert not report["target_runtime_configuration_verified"]
        assert "ab" * 32 not in output.out + output.err
    assert (target / "preferences.key").read_text() == "ab" * 32
    assert database.read_bytes() == before
    monkeypatch.setenv("QPD_MIGRATION_TEST_KEY", "cd" * 32)
    assert main(args) == 1
    output = capsys.readouterr()
    assert "not be replaced" in output.err
    assert "cd" * 32 not in output.err + output.out
    assert (target / "preferences.key").read_text() == "ab" * 32
