from __future__ import annotations

import json
from typing import Any

import pytest

from qwenpaw_data.cli.main import build_parser, main


class RecordingClient:
    """Fake SemanticConfigClient capturing calls and replaying responses."""

    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: dict[str, Any] = {}

    def __init__(self, **_: Any) -> None:
        self.timeout = 30.0

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

    def get(self, path: str, *, params: Any = None, timeout: float | None = None) -> Any:
        return self._record("GET", path, params=params, timeout=timeout)

    def post(self, path: str, *, json: Any = None, files: Any = None) -> Any:
        return self._record("POST", path, json=json, files=files)

    def put(self, path: str, *, json: Any = None) -> Any:
        return self._record("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self._record("DELETE", path)

    def list_page(self, path: str, *, params: Any = None, page: int = 1, size: int = 20) -> Any:
        return self._record("LIST", path, params=params, page=page, size=size)

    def list_all(self, path: str, *, params: Any = None, size: int = 100) -> Any:
        return self._record("LIST_ALL", path, params=params, size=size)


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    from qwenpaw_data.cli.commands import semantic

    RecordingClient.reset()
    monkeypatch.setattr(semantic, "SemanticConfigClient", RecordingClient)
    return RecordingClient


def test_semantic_command_registers_all_resources() -> None:
    help_text = build_parser().format_help()
    assert "semantic" in help_text
    for name in ("domain", "dataset", "column", "dimension", "binding", "metric", "formula"):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["semantic", name])
        assert exc_info.value.code == 2  # missing required action subcommand


def test_metric_list_maps_filters_and_prints_items(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {"LIST": {"records": [{"id": 1, "metric_name": "gaap"}], "total": 1, "page": 1}},
    )

    assert main([
        "semantic", "metric", "list",
        "--datasource-id", "pg-1", "--domain-id", "2", "--name", "gaap",
        "--page", "3", "--size", "10",
    ]) == 0

    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("LIST", "/api/semantic-config/metric-lib")
    assert kwargs["params"] == {"datasource_id": "pg-1", "domain_id": 2, "metric_name": "gaap"}
    assert kwargs["page"] == 3 and kwargs["size"] == 10

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"items": [{"id": 1, "metric_name": "gaap"}], "total": 1}


def test_domain_list_all_uses_list_all(fake_client: type[RecordingClient]) -> None:
    fake_client.reset({"LIST_ALL": {"records": [], "total": 0}})
    assert main(["semantic", "domain", "list", "--all"]) == 0
    assert fake_client.calls[0][0] == "LIST_ALL"


def test_metric_create_builds_payload_from_flags(
    fake_client: type[RecordingClient],
) -> None:
    assert main([
        "semantic", "metric", "create",
        "--domain-id", "2", "--name", "gaap", "--priority", "HIGH",
    ]) == 0

    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("POST", "/api/semantic-config/metric-lib")
    assert kwargs["json"] == {
        "domain_id": 2,
        "metric_name": "gaap",
        "priority": "HIGH",
    }


def test_create_rejects_file_combined_with_flags(
    fake_client: type[RecordingClient],
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_file = tmp_path / "metric.json"
    payload_file.write_text('{"metric_name": "x"}', encoding="utf-8")

    assert main([
        "semantic", "metric", "create",
        "--file", str(payload_file), "--name", "x",
    ]) == 1
    assert "not both" in capsys.readouterr().err
    assert fake_client.calls == []


def test_create_from_json_file(
    fake_client: type[RecordingClient],
    tmp_path: Any,
) -> None:
    payload_file = tmp_path / "formula.json"
    payload_file.write_text(
        '{"metric_id": 1, "dataset_id": 2, "expr": "sum(x)"}',
        encoding="utf-8",
    )

    assert main(["semantic", "formula", "create", "--file", str(payload_file)]) == 0
    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("POST", "/api/semantic-config/metric-formula-lib")
    assert kwargs["json"] == {"metric_id": 1, "dataset_id": 2, "expr": "sum(x)"}


def test_create_rejects_unknown_file_fields(fake_client: type[RecordingClient], tmp_path: Any, capsys):
    payload_file = tmp_path / "formula.json"
    payload_file.write_text('{"formula": "sum(x)"}', encoding="utf-8")
    assert main(["semantic", "formula", "create", "--file", str(payload_file)]) == 1
    assert "unsupported payload fields: formula" in capsys.readouterr().err
    assert fake_client.calls == []


def test_update_requires_some_change(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["semantic", "dimension", "update", "7"]) == 1
    assert "nothing to send" in capsys.readouterr().err
    assert fake_client.calls == []


def test_update_puts_to_record_path(fake_client: type[RecordingClient]) -> None:
    assert main([
        "semantic", "dimension", "update", "7", "--description", "d",
    ]) == 0
    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("PUT", "/api/semantic-config/dimension/7")
    assert kwargs["json"] == {"description": "d"}


def test_delete_requires_yes_in_non_tty(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["semantic", "metric", "delete", "3"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert fake_client.calls == []


def test_delete_with_yes_calls_api(fake_client: type[RecordingClient]) -> None:
    assert main(["semantic", "metric", "delete", "3", "--yes"]) == 0
    assert fake_client.calls[0][:2] == ("DELETE", "/api/semantic-config/metric-lib/3")


def test_binding_batch_delete_by_dataset(fake_client: type[RecordingClient]) -> None:
    assert main([
        "semantic", "binding", "delete", "--dataset-id", "12", "--yes",
    ]) == 0
    assert fake_client.calls[0][:2] == (
        "DELETE",
        "/api/semantic-config/dataset-dimension/dataset/12",
    )


def test_formula_batch_delete_by_metric(fake_client: type[RecordingClient]) -> None:
    assert main(["semantic", "formula", "delete", "--metric-id", "12", "--yes"]) == 0
    assert fake_client.calls[0][:2] == (
        "DELETE", "/api/semantic-config/metric-formula-lib/metric/12"
    )


def test_binding_delete_rejects_both_targets(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([
        "semantic", "binding", "delete", "5", "--dataset-id", "12", "--yes",
    ]) == 1
    assert "not both" in capsys.readouterr().err
    assert fake_client.calls == []


def test_import_uploads_workbook(
    fake_client: type[RecordingClient],
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workbook = tmp_path / "semantic.xlsx"
    workbook.write_bytes(b"xlsx-bytes")
    fake_client.reset({"POST": {"success": True, "summary": {"metric_lib": 2}}})

    assert main(["semantic", "import", "--file", str(workbook), "--datasource-id", "pg-1"]) == 0

    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("POST", "/api/semantic-config/import/excel")
    assert kwargs["files"]["file"][0] == "semantic.xlsx"
    assert kwargs["files"]["file"][1] == b"xlsx-bytes"
    assert kwargs["files"]["datasource_id"] == (None, "pg-1")
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_import_reports_row_errors_as_failure(fake_client, tmp_path, capsys):
    workbook = tmp_path / "semantic.xlsx"
    workbook.write_bytes(b"xlsx-bytes")
    fake_client.reset({"POST": {"success": False, "errors": [{"row": 2}]}})
    assert main(["semantic", "import", "--file", str(workbook), "--datasource-id", "pg-1"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == [{"row": 2}]


def test_import_requires_datasource_id():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["semantic", "import", "--file", "workbook.xlsx"])
    assert exc.value.code == 2


def test_weave_submit_without_wait_prints_task(
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client.reset(
        {"POST": {"task_id": "t-1", "status": "pending", "datasource_id": "pg-1"}},
    )

    assert main([
        "semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "n1",
    ]) == 0

    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("POST", "/api/semantic-config/weave-task/submit")
    assert kwargs["json"] == {"datasource_id": "pg-1", "task_name": "n1", "weave_mode": "FULL"}
    assert json.loads(capsys.readouterr().out)["task_id"] == "t-1"


def test_weave_submit_wait_polls_to_terminal_state(
    fake_client: type[RecordingClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qwenpaw_data.cli.commands import semantic

    monkeypatch.setattr(semantic, "_WEAVE_POLL_SECONDS", 0.0)
    fake_client.reset(
        {
            "POST": {"task_id": "t-2", "status": "PENDING"},
            "GET": [
                {"task_id": "t-2", "status": "RUNNING"},
                {"task_id": "t-2", "status": "SUCCESS"},
            ],
        },
    )

    assert main([
        "semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test", "--wait",
    ]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "SUCCESS"
    assert "running" in captured.err
    assert "." in captured.err
    assert [call[1] for call in fake_client.calls[1:]] == [
        "/api/semantic-config/weave-task/t-2",
        "/api/semantic-config/weave-task/t-2",
    ]


def test_weave_submit_wait_fails_on_failed_task(
    fake_client: type[RecordingClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qwenpaw_data.cli.commands import semantic

    monkeypatch.setattr(semantic, "_WEAVE_POLL_SECONDS", 0.0)
    fake_client.reset(
        {
            "POST": {"task_id": "t-3", "status": "PENDING"},
            "GET": {"task_id": "t-3", "status": "FAILED", "error_msg": "boom"},
        },
    )

    assert main([
        "semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test", "--wait",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["error_msg"] == "boom"


def test_weave_wait_rejects_missing_task_id(fake_client: type[RecordingClient], capsys):
    fake_client.reset({"POST": {"status": "PENDING"}})
    assert main(["semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test", "--wait"]) == 1
    assert "did not return a task id" in capsys.readouterr().err
    assert len(fake_client.calls) == 1


def test_weave_wait_reports_degraded_terminal_state(fake_client: type[RecordingClient], monkeypatch, capsys):
    from qwenpaw_data.cli.commands import semantic

    monkeypatch.setattr(semantic, "_WEAVE_POLL_SECONDS", 0.0)
    fake_client.reset({
        "POST": {"task_id": "t-3", "status": "RUNNING"},
        "GET": {"task_id": "t-3", "status": "DEGRADED", "error_msg": "partial failure"},
    })
    assert main(["semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test", "--wait"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "DEGRADED"


def test_weave_wait_rejects_unexpected_task(fake_client: type[RecordingClient], monkeypatch, capsys):
    from qwenpaw_data.cli.commands import semantic

    monkeypatch.setattr(semantic, "_WEAVE_POLL_SECONDS", 0.0)
    fake_client.reset({
        "POST": {"task_id": "t-3", "status": "PENDING"},
        "GET": {"task_id": "other", "status": "SUCCESS"},
    })
    assert main(["semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test", "--wait"]) == 1
    assert "unexpected task" in capsys.readouterr().err


def test_weave_wait_bounds_a_stalled_http_poll():
    import socket
    import threading
    import time

    from qwenpaw_data.cli.commands.semantic import _wait_for_weave_task
    from qwenpaw_data.host.core.semantic_config_client import SemanticConfigClient

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    release = threading.Event()

    def stall():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                release.wait(3)
        finally:
            listener.close()

    server = threading.Thread(target=stall)
    server.start()
    client = SemanticConfigClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}")
    try:
        start = time.monotonic()
        task, finished = _wait_for_weave_task(
            client, {"task_id": "t-1", "status": "RUNNING"}, timeout=0.5, poll_seconds=0
        )
        assert not finished and task["task_id"] == "t-1"
        assert time.monotonic() - start < 2
    finally:
        release.set()
        server.join(4)
    assert not server.is_alive()


def test_weave_submit_wait_times_out_with_kill_hint(
    fake_client: type[RecordingClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qwenpaw_data.cli.commands import semantic

    monkeypatch.setattr(semantic, "_WEAVE_POLL_SECONDS", 1.0)
    fake_client.reset(
        {
            "POST": {"task_id": "t-4", "status": "PENDING"},
        },
    )

    assert main([
        "semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test",
        "--wait", "--timeout", "0.01",
    ]) == 1
    captured = capsys.readouterr()
    assert "weave kill t-4" in captured.err


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_weave_submit_rejects_invalid_wait_timeout_before_submitting(
    timeout: str,
    fake_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([
        "semantic", "weave", "submit", "--datasource-id", "pg-1", "--name", "test",
        "--wait", f"--timeout={timeout}",
    ]) == 1
    assert "--timeout must be a finite positive number" in capsys.readouterr().err
    assert fake_client.calls == []


def test_weave_list_and_kill(fake_client: type[RecordingClient]) -> None:
    fake_client.reset({"LIST": {"records": [], "total": 0, "page": 1}})
    assert main(["semantic", "weave", "list", "--task-name", "n"]) == 0
    method, path, kwargs = fake_client.calls[0]
    assert (method, path) == ("LIST", "/api/semantic-config/weave-task")
    assert kwargs["params"] == {"datasource_name": None, "task_name": "n"}

    fake_client.reset()
    assert main(["semantic", "weave", "kill", "t-9"]) == 0
    assert fake_client.calls[0][:2] == ("POST", "/api/semantic-config/weave-task/t-9/kill")
