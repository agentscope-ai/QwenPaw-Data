from __future__ import annotations

import json

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
