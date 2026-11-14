"""Public migration commands exercise the real offline 15-table/file workflow."""

import json
import os
from uuid import uuid4

import pytest

from qwenpaw_data.cli.main import main, build_parser
from qwenpaw_data.host.core.migration.bundle import load_recovery_plan
from qwenpaw_data.host.core.migration.legacy_json import scan_source

from .test_legacy_migration_plan import source as make_source, USERS, CUSTOM
from .test_legacy_target_validation import initialize_target, mutate
from .test_legacy_sql_import import counts


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX saved-plan workflow")
KEY = bytes(range(32))


@pytest.fixture
def context(tmp_path, monkeypatch, capsys):
    source = make_source.__wrapped__(tmp_path)
    home = tmp_path / "target home"
    home.mkdir(mode=0o700)
    url = initialize_target(tmp_path / "target.sqlite")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    mapping, models = private / "users.json", private / "models.json"
    mapping.write_text(json.dumps({"version": 1, "users": USERS}))
    models.write_text(json.dumps({"version": 1, "users": {"old": {CUSTOM: 77788}}}))
    for name in (
        "QWENPAW_DATA_PREFERENCES_MASTER_SECRET",
        "QWENPAW_DATA_PREFS_MASTER_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    monkeypatch.setenv(
        "QWENPAW_DATA_HOME", str(source)
    )  # Must never create source logs.
    monkeypatch.setenv("MIGRATION_CLI_TEST_KEY", KEY.hex())
    monkeypatch.setenv("MIGRATION_CLI_TEST_DB", url)
    target = [
        "--target-home",
        str(home),
        "--target-db-env",
        "MIGRATION_CLI_TEST_DB",
        "--target-key-env",
        "MIGRATION_CLI_TEST_KEY",
        "--target-stopped",
    ]
    assert (
        main(
            [
                "migration",
                "backup",
                "--source-home",
                str(source),
                "--destination",
                str(tmp_path / "backup"),
                "--source-stopped",
            ]
        )
        == 0
    )
    assert main(["migration", "provision-key", *target]) == 0
    run = uuid4().hex
    path = private / "recovery.enc"
    args = [
        "migration",
        "prepare",
        *target,
        "--source-home",
        str(source),
        "--backup",
        str(tmp_path / "backup"),
        "--source-stopped",
        "--identity-map",
        str(mapping),
        "--model-context-sizes",
        str(models),
        "--context-trigger-ratio",
        "0.8",
        "--context-reserve-ratio",
        "0.1",
        "--run-id",
        run,
        "--recovery-plan",
        str(path),
        "--deployment-config-confirmed",
    ]
    capsys.readouterr()
    return dict(
        source=source,
        home=home,
        url=url,
        target=target,
        run=run,
        path=path,
        prepare=args,
    )


def command(context, action, *, run=None):
    args = [
        "migration",
        action,
        *context["target"],
        "--recovery-plan",
        str(context["path"]),
    ]
    if action != "check-rollback":
        args += ["--confirm-run-id", run or context["run"]]
    if action in {"apply", "recover"}:
        args += ["--deployment-config-confirmed"]
    return args


def prepare(context, capsys):
    assert main(context["prepare"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "saved_recovery_plan"
    assert report["run_id"] == context["run"]
    return load_recovery_plan(
        context["path"],
        database_url=context["url"],
        target_home=context["home"],
        key=KEY,
    )


def test_cli_full_prepare_apply_recover_check_and_rollback(context, capsys):
    before = scan_source(context["source"]).digest
    saved = prepare(context, capsys)
    assert len({r.table for r in saved.plan.rows}) == 15 and len(saved.plan.files) == 2
    assert counts(context["url"])["sessions"] == 0
    assert context["path"].stat().st_mode & 0o777 == 0o600
    assert scan_source(context["source"]).digest == before
    original = context["path"].read_bytes()
    context["source"].rename(context["source"].with_name("offline"))
    for action, status in (("apply", "committed"), ("recover", "already_committed")):
        assert main(command(context, action)) == 0
        output = capsys.readouterr()
        report = json.loads(output.out)
        assert report["status"] == status
        assert report["key_source_checked"] == "explicit_target_home_file"
        assert not report["remote_runtime_configuration_verified"]
        assert "synthetic-private-key" not in output.out + output.err
    assert main(command(context, "check-rollback")) == 0
    assert json.loads(capsys.readouterr().out)["native_dependencies_clear"]
    for status in ("rolled_back", "already_rolled_back"):
        assert main(command(context, "rollback")) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == status and report["published_files_absent"]
    assert context["path"].read_bytes() == original
    assert all(not (context["home"] / f.destination).exists() for f in saved.plan.files)
    assert main(command(context, "apply")) == 1
    assert "forward replay is forbidden" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["apply", "recover", "rollback"])
def test_cli_requires_matching_run_before_any_migration_write(context, capsys, action):
    prepare(context, capsys)
    before = counts(context["url"])
    assert main(command(context, action, run="f" * 32)) == 1
    assert "confirm-run-id" in capsys.readouterr().err
    assert counts(context["url"]) == before
    assert not (context["home"] / ".legacy-migrations").exists()


@pytest.mark.parametrize("action", ["apply", "recover", "rollback"])
def test_cli_confirmation_is_rechecked_by_coordinator(
    context, capsys, monkeypatch, action
):
    from dataclasses import replace
    import importlib
    import qwenpaw_data.host.core.migration.bundle as bundle

    importlib.import_module("qwenpaw_data.host.core.migration.coordinator")
    importlib.import_module("qwenpaw_data.host.core.migration.rollback")
    prepare(context, capsys)
    original = bundle.load_recovery_plan
    fake_id = "e" * 32
    # Simulate a different pre-read plan. The coordinator's bound original loader
    # must reject the confirmed ID, not execute whichever plan is present later.
    monkeypatch.setattr(
        bundle,
        "load_recovery_plan",
        lambda *a, **kw: replace(original(*a, **kw), run_id=fake_id),
    )
    before = counts(context["url"])
    assert main(command(context, action, run=fake_id)) == 1
    assert "Confirmed run ID differs" in capsys.readouterr().err
    assert counts(context["url"]) == before
    assert not (context["home"] / ".legacy-migrations").exists()


@pytest.mark.parametrize(
    "action", ["prepare", "apply", "recover", "rollback", "check-rollback"]
)
def test_cli_write_or_check_requires_target_stop_ack(context, action):
    args = list(context["prepare"]) if action == "prepare" else command(context, action)
    args.remove("--target-stopped")
    if (
        action == "prepare"
    ):  # Shared plan flag stays optional in parser, required by handler.
        assert main(args) == 1
    else:
        with pytest.raises(SystemExit) as error:
            build_parser().parse_args(args)
        assert error.value.code == 2
    assert counts(context["url"])["legacy_import_targets"] == 0


def test_prepare_never_replans_or_replaces_existing_bundle(context, capsys):
    prepare(context, capsys)
    original = context["path"].read_bytes()
    before = counts(context["url"])
    assert main(context["prepare"]) == 1
    assert "never reprepare" in capsys.readouterr().err
    assert context["path"].read_bytes() == original and counts(context["url"]) == before


@pytest.mark.parametrize("action", ["prepare", "apply", "recover"])
def test_cli_import_refuses_conflicting_runtime_key(
    context, capsys, monkeypatch, action
):
    if action != "prepare":
        prepare(context, capsys)
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", "ef" * 32)
    before = counts(context["url"])
    assert (
        main(context["prepare"] if action == "prepare" else command(context, action))
        == 1
    )
    output = capsys.readouterr()
    assert "differs" in output.err and "ef" * 32 not in output.err
    assert counts(context["url"]) == before


def test_rollback_can_undo_import_even_if_runtime_configuration_has_changed(
    context, capsys, monkeypatch
):
    prepare(context, capsys)
    assert main(command(context, "apply")) == 0
    capsys.readouterr()
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", "ef" * 32)
    assert main(command(context, "rollback")) == 0
    assert json.loads(capsys.readouterr().out)["key_source_checked"] is None


def test_unauthorized_target_is_reported_without_metadata_token(context, capsys):
    mutate(context["url"], "DELETE FROM role_bindings")
    assert main(context["prepare"]) == 2
    assert not json.loads(capsys.readouterr().out)["target_checks_passed"]
    assert counts(context["url"])["legacy_import_targets"] == 0
    assert not context["path"].exists()


def test_cli_recover_reuses_original_ciphertext_after_lost_sql_response(
    context, capsys, monkeypatch
):
    import qwenpaw_data.host.core.migration.coordinator as coordinator

    saved = prepare(context, capsys)
    original = coordinator.commit_sql_rows

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic lost response")

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "commit_sql_rows", lost)
        assert main(command(context, "apply")) == 1
    capsys.readouterr()
    assert main(command(context, "recover")) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        report["status"] == "already_committed"
        and report["plan_digest"] == saved.digest
    )
