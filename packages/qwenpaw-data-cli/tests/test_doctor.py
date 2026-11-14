"""qwenpaw-data doctor 各检查项的单元测试（mock 网络，不依赖真实环境）。"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from qwenpaw_data.cli.commands import doctor


def _run_handle() -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = doctor.handle(argparse.Namespace())
    return code, buf.getvalue()


def test_all_ok_returns_zero(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_WORKSPACE", raising=False)
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
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE", "local")
    result = doctor._check_docker_daemon()
    assert result.status == doctor._WARN
    assert result.hints  # 平台相关引导文案存在


def test_docker_daemon_missing_fails_for_default_workspace(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.delenv("QWENPAW_DATA_WORKSPACE", raising=False)
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
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    result = doctor._check_neo4j()
    assert result.status == doctor._FAIL


def test_neo4j_checks_configured_uri_not_local_compose_port(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://graph.example:9876")
    monkeypatch.setenv("NEO4J_BOLT_PORT", "1234")
    calls = []
    monkeypatch.setattr(doctor, "_port_reachable", lambda host, port: calls.append((host, port)) or True)
    result = doctor._check_neo4j()
    assert calls == [("graph.example", 9876)]
    assert result.status == doctor._OK
    assert "authentication and database not checked" in result.detail


@pytest.mark.parametrize("backend", ["holoage", "holo_age", " HOLO-AGE "])
def test_neo4j_is_not_required_for_hologres(monkeypatch, backend):
    monkeypatch.setenv("GRAPH_BACKEND", backend)
    monkeypatch.setattr(doctor, "_port_reachable", lambda *args: pytest.fail("unexpected Neo4j probe"))
    result = doctor._check_neo4j()
    assert result.status == doctor._INFO
    assert "not checked" in result.detail


@pytest.mark.parametrize("uri", [
    "bolt://user:secret@graph.example:7687", "bolt://graph.example:99999", "https://graph.example"
])
def test_invalid_neo4j_uri_fails_without_exposing_credentials(monkeypatch, uri):
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", uri)
    result = doctor._check_neo4j()
    assert result.status == doctor._FAIL
    assert "secret" not in result.detail


def test_databridge_unreachable(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:9")
    result = doctor._check_databridge()
    assert result.status == doctor._FAIL


def test_workspace_backend_invalid(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE", "bogus")
    result = doctor._check_workspace_backend()
    assert result.status == doctor._FAIL


def test_doctor_registered_in_cli():
    from qwenpaw_data.cli import build_parser

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
    from qwenpaw_data.host.core import semantic_config_client

    class Client:
        def get(self, path):
            assert path == "/api/v1/config"
            return {
                "llm": {"model": "chat", "api_key": "super-secret"},
                "embedding": {"model": "embed", "api_key": "super-secret"},
                "errors": {},
            }

    monkeypatch.setattr(semantic_config_client, "SemanticConfigClient", Client)
    result = doctor._check_model_config()
    assert result.status == doctor._OK
    assert "super-secret" not in result.detail
    assert "connectivity not tested" in result.detail


@pytest.mark.parametrize(("errors", "expected"), [
    ({"embedding": "missing"}, doctor._FAIL),
    ({"llm": "missing"}, doctor._WARN),
])
def test_model_check_distinguishes_publication_and_chat(monkeypatch, errors, expected):
    from qwenpaw_data.host.core import semantic_config_client

    class Client:
        def get(self, path):
            return {
                "llm": {"model": "chat", "api_key": "****"},
                "embedding": {"model": "embed", "api_key": "****"},
                "errors": errors,
            }

    monkeypatch.setattr(semantic_config_client, "SemanticConfigClient", Client)
    assert doctor._check_model_config().status == expected


def test_model_check_reports_auth_failure_as_unchecked(monkeypatch):
    from qwenpaw_data.host.core import semantic_config_client

    class Client:
        def get(self, path):
            raise semantic_config_client.SemanticConfigClientError("secret", status_code=403)

    monkeypatch.setattr(semantic_config_client, "SemanticConfigClient", Client)
    result = doctor._check_model_config()
    assert result.status == doctor._WARN
    assert "could not be checked" in result.detail
    assert "secret" not in result.detail


def test_auth_check_reports_presence_without_token(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "server-secret")
    monkeypatch.setenv("QWENPAW_DATA_CLIENT_API_TOKEN", "client-secret")
    result = doctor._check_auth_config()
    assert result.status == doctor._OK
    assert "server-secret" not in result.detail
    assert "client-secret" not in result.detail


def test_mcp_check_finds_databridge(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
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
