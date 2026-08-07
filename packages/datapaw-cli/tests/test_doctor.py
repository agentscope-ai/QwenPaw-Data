"""datapaw doctor 各检查项的单元测试（mock 网络，不依赖真实环境）。"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from datapaw.cli.commands import doctor


def _run_handle() -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = doctor.handle(argparse.Namespace())
    return code, buf.getvalue()


def test_all_ok_returns_zero(monkeypatch):
    monkeypatch.delenv("DATAPAW_WORKSPACE", raising=False)
    ok = doctor.CheckResult("x", doctor._OK, "fine")
    with patch.object(doctor, "_CHECKS", [lambda: ok]):
        code, out = _run_handle()
    assert code == 0
    assert "OK" in out


def test_fail_returns_one(monkeypatch):
    fail = doctor.CheckResult("x", doctor._FAIL, "broken", ["fix it"])
    with patch.object(doctor, "_CHECKS", [lambda: fail]):
        code, out = _run_handle()
    assert code == 1
    assert "FAIL" in out


def test_warn_does_not_fail():
    warn = doctor.CheckResult("x", doctor._WARN, "meh")
    with patch.object(doctor, "_CHECKS", [lambda: warn]):
        code, _ = _run_handle()
    assert code == 0


def test_docker_daemon_missing_gives_guidance(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.setenv("DATAPAW_WORKSPACE", "local")
    result = doctor._check_docker_daemon()
    assert result.status == doctor._WARN
    assert result.hints  # 平台相关引导文案存在


def test_docker_daemon_missing_fails_for_default_workspace(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.delenv("DATAPAW_WORKSPACE", raising=False)
    result = doctor._check_docker_daemon()
    assert result.status == doctor._FAIL


def test_docker_compose_reports_v2_plugin(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "_command_version",
        lambda *_: ("/docker", "Docker Compose version v2.39.1"),
    )
    assert doctor._check_docker_compose().status == doctor._OK


def test_docker_compose_missing_is_actionable_failure(monkeypatch):
    monkeypatch.setattr(doctor, "_command_version", lambda *_: ("/docker", None))
    result = doctor._check_docker_compose()
    assert result.status == doctor._FAIL
    assert result.hints


def test_neo4j_unreachable(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_PORT", "1")  # 必然连不上的端口
    result = doctor._check_neo4j()
    assert result.status == doctor._FAIL


def test_databridge_unreachable(monkeypatch):
    monkeypatch.setenv("DATAPAW_CM_BASE_URL", "http://127.0.0.1:9")
    result = doctor._check_databridge()
    assert result.status == doctor._FAIL


def test_workspace_backend_invalid(monkeypatch):
    monkeypatch.setenv("DATAPAW_WORKSPACE", "bogus")
    result = doctor._check_workspace_backend()
    assert result.status == doctor._FAIL


def test_doctor_registered_in_cli():
    from datapaw.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["doctor"])
    assert args.handler is doctor.handle


def test_supported_python_is_ok():
    result = doctor._check_python()
    assert result.status == doctor._OK


def test_node_requires_supported_baseline(monkeypatch):
    monkeypatch.setattr(doctor, "_command_version", lambda *_: ("/node", "v20.0.0"))
    assert doctor._check_node().status == doctor._FAIL

    monkeypatch.setattr(doctor, "_command_version", lambda *_: ("/node", "v22.22.0"))
    assert doctor._check_node().status == doctor._OK


def test_model_check_does_not_expose_api_key(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "demo-model")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret")
    result = doctor._check_model_config()
    assert result.status == doctor._OK
    assert "super-secret" not in result.detail


def test_auth_check_reports_presence_without_token(monkeypatch):
    monkeypatch.setenv("DATAPAW_API_TOKEN", "server-secret")
    monkeypatch.setenv("DATAPAW_CLIENT_API_TOKEN", "client-secret")
    result = doctor._check_auth_config()
    assert result.status == doctor._OK
    assert "server-secret" not in result.detail
    assert "client-secret" not in result.detail


def test_mcp_check_finds_databridge(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPAW_HOME", str(tmp_path))
    path = tmp_path / "host" / "workspace" / ".mcp"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"name": "databridge"}]), encoding="utf-8")
    assert doctor._check_mcp_config().status == doctor._OK


def test_json_report_contains_no_secret_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    result = doctor.CheckResult("Model config", doctor._OK, "credentials=set")
    with patch.object(doctor, "_CHECKS", [lambda: result]):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = doctor.handle(argparse.Namespace(json=True))
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert "must-not-leak" not in buf.getvalue()
