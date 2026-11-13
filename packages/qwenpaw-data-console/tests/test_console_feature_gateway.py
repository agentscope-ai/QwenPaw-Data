"""Product features must cross the public gateway, not only work on Host."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.console import create_app

HEADERS = {"X-QwenPaw-Console": "1"}
# Deliberately independent of the implementation's regex policy.
FEATURE_CASES = [
    ("skills/agents/default", "GET POST"),
    ("skills/agents/default/import", "POST"),
    ("skills/agents/default/my-skill", "GET PUT DELETE"),
    ("skills/agents/default/my-skill/enable", "POST"),
    ("skills/agents/default/my-skill/disable", "POST"),
    ("skills/agents/default/my-skill/revisions", "GET"),
    ("skills/agents/default/my-skill/revisions/1", "GET"),
    ("skills/agents/default/my-skill/revisions/0", "GET"),
    ("skills/agents/default/my-skill/revisions/0/revert", "POST"),
    ("skills/agents/default/my-skill/revisions/1/revert", "POST"),
    ("mcp", "GET POST"),
    ("mcp/my-mcp", "GET PUT DELETE"),
    ("mcp/my-mcp/tools", "GET PUT"),
    ("mcp/my-mcp/enable", "POST"),
    ("mcp/my-mcp/disable", "POST"),
    ("system/channel-config/", "GET"),
    ("system/channel-config/dingtalk", "PUT"),
    ("system/channel-config/dingtalk/test", "POST"),
    ("system/channel-config/wechat/qrcode", "GET"),
    ("system/channel-config/wechat/qrcode/status", "GET"),
    ("settlement/cards", "GET"),
    ("settlement/skill-proposals", "GET"),
    ("sessions/s1/skill-drafts", "GET POST"),
    ("sessions/s1/skill-drafts/d1", "GET"),
    ("sessions/s1/skill-drafts/d1/revise", "POST"),
    ("sessions/s1/skill-drafts/d1/confirm", "POST"),
    ("sessions/s1/skill-drafts/d1/dismiss", "POST"),
    ("sessions/s1/settlement/skill-proposals", "GET"),
    ("sessions/s1/settlement/skill-proposals/p1", "GET"),
    ("sessions/s1/settlement/skill-proposals/p1/accept", "POST"),
    ("sessions/s1/settlement/skill-proposals/p1/reject", "POST"),
    ("sessions/s1/files/compare", "POST"),
    ("sessions/s1/files/diff", "POST"),
    ("sessions/s1/files/compare/recommendations", "POST"),
    ("sessions/s1/files/upload", "POST"),
    ("sessions/s1/files/share", "POST"),
]


@pytest.fixture
def frontend(tmp_path):
    directory = tmp_path / "frontend"
    directory.mkdir()
    (directory / "index.html").write_text("<main>Gateway integration fixture</main>")
    return directory


@pytest.mark.parametrize("path,methods", FEATURE_CASES)
def test_feature_route_method_and_origin_contract(frontend, path, methods):
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer gateway-service-token"
        assert "x-qwenpaw-console" not in request.headers
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        host_url="http://127.0.0.1:9999", token="gateway-service-token",
        transport=httpx.MockTransport(upstream), frontend=frontend,
    )
    with TestClient(app) as client:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            before = len(calls)
            response = client.request(
                method, "/api/v1/" + path, params={"probe": "1"},
                headers={**HEADERS, "Authorization": "Bearer untrusted-browser-token"},
                json={"probe": True},
            )
            if method in methods.split():
                assert response.status_code == 200
                assert len(calls) == before + 1
                assert calls[-1].url.path == "/api/v1/" + path
                assert calls[-1].url.params["probe"] == "1"
                assert json.loads(calls[-1].content) == {"probe": True}
            else:
                assert response.status_code == 405
                assert len(calls) == before
        before = len(calls)
        method = methods.split()[0]
        assert client.request(method, "/api/v1/" + path).status_code == 403
        assert client.request(method, "/api/v1/" + path, headers={
            **HEADERS, "Origin": "https://untrusted.invalid",
        }).status_code == 403
        assert len(calls) == before


@pytest.mark.parametrize("path", [
    "skills/agents/default/x/private", "skills/agents/default/x/revisions/not-a-number",
    "skills/agents/default/x/revisions/-1/revert", "mcp/x/private",
    "system/channel-config/private", "system/channel-config/dingtalk/qrcode",
    "channels/reload", "settlement/private", "sessions/s1/skill-drafts/d1/private",
    "sessions/s1/settlement/skill-proposals/p1/private", "sessions/s1/files/delete",
    "mcp/%252e%252e", "mcp/%2e%2e%2fsecret", "mcp/a%5cb", "mcp/a%3fb",
    "mcp/a%23b", "mcp/a%00b", "mcp/%20name", "mcp/name%20", "mcp/a%2ftools",
    "mcp//name",
])
def test_unknown_or_ambiguous_feature_paths_never_reach_host(frontend, path):
    def upstream(_request):
        pytest.fail("Rejected path reached upstream")

    app = create_app(
        host_url="http://127.0.0.1:9999", token="test-token",
        transport=httpx.MockTransport(upstream), frontend=frontend,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/" + path, headers=HEADERS).status_code == 404


def test_feature_uploads_names_and_upstream_denials(frontend):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "Host access denied"})

    app = create_app(
        host_url="http://127.0.0.1:9999", token="test-token",
        transport=httpx.MockTransport(upstream), frontend=frontend,
    )
    with TestClient(app) as client:
        for path in ("skills/agents/default/import", "sessions/s1/files/upload"):
            response = client.post("/api/v1/" + path, headers=HEADERS,
                                   files={"file": ("fixture.txt", b"test bytes")})
            assert response.status_code == 403
            assert response.json() == {"detail": "Host access denied"}
            assert calls[-1].headers["content-type"].startswith("multipart/form-data;")
            assert b"test bytes" in calls[-1].content
        response = client.get("/api/v1/skills/agents/default/分析.v1", headers=HEADERS)
        assert response.status_code == 403
        assert calls[-1].url.path.endswith("/分析.v1")
        config = client.get("/portal/config", headers=HEADERS).json()
        assert config["capabilities"]["skillsHub"] is True
        assert config["capabilities"]["channels"] is True
        assert config["capabilities"]["mcpHub"] is False


def test_feature_contract_has_matching_host_routes():
    # Inspect the contract without starting Host or making external calls.
    from qwenpaw_data.host.core.api.app import create_app as create_host

    paths = create_host().openapi()["paths"]
    for path, methods in FEATURE_CASES:
        matches = [operations for template, operations in paths.items()
                   if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), "/api/v1/" + path)]
        assert matches, path
        for method in methods.split():
            assert any(method.lower() in operations for operations in matches), (path, method)


@pytest.fixture
def live_host(tmp_path):
    """Real disposable local Host; no model, graph or channel credentials."""
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("QWENPAW_DATA_", "TRACING_", "OTEL_", "OPENAI_",
                                  "ANTHROPIC_", "DASHSCOPE_", "FC_", "ACS_"))}
    token = "disposable-feature-test-token"
    env.update(
        QWENPAW_DATA_HOME=str(tmp_path / "home"),
        QWENPAW_DATA_ENV_FILE=str(tmp_path / "absent.env"),
        QWENPAW_DATA_AUTH_MODE="local", QWENPAW_DATA_API_TOKEN=token,
        QWENPAW_DATA_DB_URL=f"sqlite+aiosqlite:///{tmp_path / 'host.sqlite'}",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(str(path) for path in root.glob("packages/*/src")),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (tmp_path / "host.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "qwenpaw_data.host.core.api.app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(trust_env=False, timeout=2) as client:
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        if client.get(url + "/api/v1/auth/status").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    log.seek(0)
                    pytest.fail(f"Host startup failed: {log.read()}")
            yield url, token
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_real_host_skill_lifecycle_and_product_reads_through_gateway(frontend, live_host):
    url, token = live_host
    app = create_app(host_url=url, token=token, frontend=frontend)
    with TestClient(app, headers=HEADERS) as client:
        base = "/api/v1/skills/agents/default"
        content = "---\nname: gateway-skill\ndescription: Gateway lifecycle test\n---\n\n# Original\n"
        response = client.post(base, json={"name": "gateway-skill", "content": content})
        assert response.status_code == 200, response.text
        skill = base + "/gateway-skill"
        assert client.get(skill).json()["content"] == content
        assert "gateway-skill" in {s["name"] for s in client.get(base).json()["skills"]}
        changed = content.replace("# Original", "# Edited")
        response = client.put(skill, json={"name": "gateway-skill", "content": changed,
                                          "base_content": content})
        assert response.status_code == 200, response.text
        assert client.get(skill).json()["content"] == changed
        conflict = client.put(skill, json={"name": "gateway-skill", "content": content,
                                          "base_content": content})
        assert conflict.status_code == 409, conflict.text
        revisions = client.get(skill + "/revisions").json()["revisions"]
        assert len(revisions) >= 2
        first = min(r["revision_no"] for r in revisions)
        assert client.get(f"{skill}/revisions/{first}").status_code == 200
        assert client.post(f"{skill}/revisions/{first}/revert").status_code == 200
        assert client.get(skill).json()["content"] == content
        assert client.post(skill + "/disable").json()["enabled"] is False
        assert client.post(skill + "/enable").json()["enabled"] is True
        assert client.delete(skill).status_code == 200
        assert client.get(skill).status_code == 404

        for path, key in [("mcp", "mcps"), ("settlement/cards", "cards"),
                          ("settlement/skill-proposals", "skill_proposals")]:
            response = client.get("/api/v1/" + path)
            assert response.status_code == 200, response.text
            assert isinstance(response.json()[key], list)
        channels = client.get("/api/v1/system/channel-config/")
        assert channels.status_code == 200, channels.text
        assert channels.json()
        assert all(not cfg["enabled"] for cfg in channels.json().values())
        response = client.put("/api/v1/system/channel-config/dingtalk", json={
            "enabled": False, "client_id": "gateway-fixture",
            "client_secret": "disposable-channel-test-secret",
        })
        assert response.status_code == 200, response.text
        assert "disposable-channel-test-secret" not in response.text
        response = client.get("/api/v1/system/channel-config/")
        assert response.json()["dingtalk"]["client_id"] == "gateway-fixture"
        assert response.json()["dingtalk"]["enabled"] is False
        assert "disposable-channel-test-secret" not in response.text

        mcp = {"name": "gateway-mcp", "enabled": False, "is_stateful": False,
               "mcp_config": {"type": "http_mcp", "url": "http://127.0.0.1:9/mcp"}}
        response = client.post("/api/v1/mcp", json=mcp)
        assert response.status_code == 200, response.text
        assert client.get("/api/v1/mcp/gateway-mcp").json()["enabled"] is False
        response = client.put("/api/v1/mcp/gateway-mcp/tools", json={"disable_tools": ["danger"]})
        assert response.status_code == 200, response.text
        assert client.get("/api/v1/mcp/gateway-mcp").json()["disable_tools"] == ["danger"]
        assert client.delete("/api/v1/mcp/gateway-mcp").status_code == 200
        assert client.get("/api/v1/mcp/gateway-mcp").status_code == 404

        session = client.post("/api/v1/sessions", json={}).json()["session"]["id"]
        assert client.get(f"/api/v1/sessions/{session}/skill-drafts").json()["skill_drafts"] == []
        for tail in ("skill-drafts/missing", "settlement/skill-proposals/missing"):
            response = client.get(f"/api/v1/sessions/{session}/{tail}")
            assert response.status_code == 404
            assert "Unsupported portal route" not in response.text

        files = f"/api/v1/sessions/{session}/files"
        uploaded = []
        for name, text in [("first.txt", b"first line\n"), ("second.txt", b"changed line\n")]:
            response = client.post(files + "/upload", files={"file": (name, text)})
            assert response.status_code == 200, response.text
            uploaded.append(response.json()["path"])
            downloaded = client.get(files + "/access", params={"path": uploaded[-1], "purpose": "download"})
            assert downloaded.content == text
        response = client.post(files + "/diff", json={"target_path": uploaded[0], "reference_path": uploaded[1]})
        assert response.status_code == 200, response.text
        response = client.get(files + "/access", params={"path": "../../host.sqlite", "purpose": "download"})
        assert response.status_code in {400, 403, 404}
