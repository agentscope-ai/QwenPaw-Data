from __future__ import annotations

import json

import pytest

from datapaw.cli.main import build_parser, main
from datapaw.host.core.cm_client import CMDatasource, CMDatasourceList, CMClientError


def test_datasource_command_is_registered_without_base_url_option() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert "datasource" in help_text
    assert "data-source" not in help_text
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["datasource", "list", "--base-url", "https://cm.test"])
    assert exc_info.value.code == 2


def test_datasource_list_outputs_masked_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datapaw.cli.commands import datasource

    secrets = {
        "password": "postgres-password",
        "mysql_password": "mysql-password",
        "access_key_id": "odps-access-id",
        "access_key_secret": "odps-access-secret",
        "sts_token": "odps-sts-token",
    }
    result = CMDatasourceList(
        items=[
            CMDatasource(
                datasource_id="postgresql-a",
                datasource_name="Primary",
                datasource_type="postgresql",
                config={
                    "host": "db.example.test",
                    "port": 5432,
                    "dbname": "sales",
                    "user": "analyst",
                    "password": secrets["password"],
                },
            ),
            CMDatasource(
                datasource_id="mysql-b",
                datasource_name="Replica",
                datasource_type="mysql",
                config={
                    "password": secrets["mysql_password"],
                    "database": "reporting",
                },
            ),
            CMDatasource(
                datasource_id="odps-c",
                datasource_name="Warehouse",
                datasource_type="odps",
                config={
                    "endpoint": "https://service.odps.example.test/api",
                    "project": "warehouse",
                    "access_key_id": secrets["access_key_id"],
                    "access_key_secret": secrets["access_key_secret"],
                    "sts_token": secrets["sts_token"],
                },
            ),
            CMDatasource(
                datasource_id="null-config",
                datasource_name=None,
                datasource_type=None,
                config=None,
            ),
            CMDatasource(
                datasource_id="empty-password",
                datasource_name="No password",
                datasource_type="mysql",
                config={"password": ""},
            ),
        ],
        total=5,
    )

    class FakeClient:
        def list_datasources(self) -> CMDatasourceList:
            return result

    monkeypatch.setattr(datasource, "ContextManagerClient", FakeClient)

    assert main(["datasource", "list"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["total"] == 5
    assert payload["items"][0]["config"] == {
        "host": "db.example.test",
        "port": 5432,
        "dbname": "sales",
        "user": "analyst",
        "password": "******",
    }
    assert payload["items"][1]["config"] == {
        "password": "******",
        "database": "reporting",
    }
    assert payload["items"][2]["config"] == {
        "endpoint": "https://service.odps.example.test/api",
        "project": "warehouse",
        "access_key_id": "******",
        "access_key_secret": "******",
        "sts_token": "******",
    }
    assert payload["items"][3]["config"] is None
    assert payload["items"][4]["config"]["password"] == ""
    for secret in secrets.values():
        assert secret not in captured.out
        assert secret not in captured.err


def test_datasource_list_reports_safe_client_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datapaw.cli.commands import datasource

    class FailingClient:
        def list_datasources(self) -> CMDatasourceList:
            raise CMClientError("CM datasource response has invalid datasource_id")

    monkeypatch.setattr(datasource, "ContextManagerClient", FailingClient)

    assert main(["datasource", "list"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid datasource_id" in captured.err
