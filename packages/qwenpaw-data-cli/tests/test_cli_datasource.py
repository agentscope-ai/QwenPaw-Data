from __future__ import annotations

import json
from typing import Any

import pytest

from qwenpaw_data.cli.main import build_parser, main
from qwenpaw_data.host.core.cm_client import CMDatasource, CMDatasourceList, CMClientError


class RecordingClient:
    """Fake SemanticConfigClient capturing calls and replaying responses."""

    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: dict[str, Any] = {}

    def __init__(self, **_: Any) -> None:
        pass

    @classmethod
    def reset(cls, responses: dict[str, Any] | None = None) -> None:
        cls.calls = []
        cls.responses = responses or {}

    def _record(self, method: str, path: str, **kwargs: Any) -> Any:
        type(self).calls.append((method, path, kwargs))
        for key in (f"{method} {path}", method, path):
            if key in type(self).responses:
                value = type(self).responses[key]
                return value.pop(0) if isinstance(value, list) else value
        return {}

    def get(self, path: str, *, params: Any = None) -> Any:
        return self._record("GET", path, params=params)

    def post(self, path: str, *, json: Any = None, files: Any = None) -> Any:
        return self._record("POST", path, json=json, files=files)

    def put(self, path: str, *, json: Any = None) -> Any:
        return self._record("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self._record("DELETE", path)


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    from qwenpaw_data.cli.commands import datasource

    RecordingClient.reset()
    monkeypatch.setattr(datasource, "SemanticConfigClient", RecordingClient)
    return RecordingClient


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
    from qwenpaw_data.cli.commands import datasource

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
    from qwenpaw_data.cli.commands import datasource

    class FailingClient:
        def list_datasources(self) -> CMDatasourceList:
            raise CMClientError("CM datasource response has invalid datasource_id")

    monkeypatch.setattr(datasource, "ContextManagerClient", FailingClient)

    assert main(["datasource", "list"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid datasource_id" in captured.err


def test_datasource_get_hides_config_by_default(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {
            "GET": {
                "datasource_id": "pg-1",
                "datasource_name": "demo",
                "datasource_type": "postgresql",
                "config": {"host": "db", "password": "secret"},
            },
        },
    )

    assert main(["datasource", "get", "pg-1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert "config" not in payload
    assert fake_client.calls[0][:2] == ("GET", "/api/semantic-config/datasource/pg-1")


def test_datasource_get_show_config_masks_secrets(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {
            "GET": {
                "datasource_id": "pg-1",
                "config": {"host": "db", "password": "secret"},
            },
        },
    )

    assert main(["datasource", "get", "pg-1", "--show-config"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["config"] == {"host": "db", "password": "******"}
    assert "secret" not in captured.out


def test_datasource_create_with_test_aborts_on_failed_connection(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {"POST": {"success": False, "message": "connection refused", "elapsed_ms": 3}},
    )

    assert main([
        "datasource", "create",
        "--name", "demo", "--type", "postgresql",
        "--config", '{"host": "db", "password": "secret"}',
        "--test",
    ]) == 1

    captured = capsys.readouterr()
    assert "connection test failed" in captured.err
    # only the test-connection call happened; nothing was created
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0][1].endswith("/datasource/test-connection")


def test_datasource_create_posts_config_and_masks_output(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {
            "POST": {
                "datasource_id": "postgresql-x",
                "datasource_name": "demo",
                "datasource_type": "postgresql",
                "config": {"host": "db", "password": "secret"},
            },
        },
    )

    assert main([
        "datasource", "create",
        "--name", "demo", "--type", "postgresql",
        "--config", '{"host": "db", "password": "secret"}',
    ]) == 0

    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("POST", "/api/semantic-config/datasource")
    assert kwargs["json"]["config"] == {"host": "db", "password": "secret"}

    captured = capsys.readouterr()
    assert json.loads(captured.out)["config"]["password"] == "******"
    assert "secret" not in captured.out


def test_datasource_update_requires_a_change(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["datasource", "update", "pg-1"]) == 1
    assert "nothing to update" in capsys.readouterr().err
    assert fake_client.calls == []


def test_datasource_update_puts_partial_payload(
    fake_client: type[RecordingClient],
) -> None:
    assert main(["datasource", "update", "pg-1", "--name", "renamed"]) == 0
    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("PUT", "/api/semantic-config/datasource/pg-1")
    assert kwargs["json"] == {"datasource_name": "renamed"}


def test_datasource_delete_requires_yes_in_non_tty(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["datasource", "delete", "pg-1"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert fake_client.calls == []


def test_datasource_delete_with_yes(fake_client: type[RecordingClient]) -> None:
    assert main(["datasource", "delete", "pg-1", "--yes"]) == 0
    assert fake_client.calls[0][:2] == ("DELETE", "/api/semantic-config/datasource/pg-1")


def test_datasource_test_saved_id_exit_code_follows_success(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset({"POST": {"success": True, "message": "ok", "elapsed_ms": 5}})
    assert main(["datasource", "test", "pg-1"]) == 0
    assert fake_client.calls[0][:2] == (
        "POST",
        "/api/semantic-config/datasource/pg-1/test-connection",
    )

    fake_client.reset({"POST": {"success": False, "message": "nope", "elapsed_ms": 5}})
    assert main(["datasource", "test", "pg-1"]) == 1
    capsys.readouterr()


def test_datasource_test_rejects_id_and_adhoc_config_together(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([
        "datasource", "test", "pg-1",
        "--type", "postgresql", "--config", "{}",
    ]) == 1
    assert "not both" in capsys.readouterr().err
    assert fake_client.calls == []
