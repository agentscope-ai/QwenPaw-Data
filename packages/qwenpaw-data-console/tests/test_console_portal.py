import json
import os

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from qwenpaw_data.console import ConsoleConfig, create_app


def create_portal(home, host_url, token, transport=None, **kwargs):
    return create_app(host_url=host_url, token=token, transport=transport, **kwargs)


@pytest.fixture
def frontend(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text('<div id="root">GEO Console</div>')
    (root / "assets").mkdir()
    (root / "assets/app.js").write_text("// built app")
    return root


def test_portal_keeps_credentials_server_side_and_proxies_sse(tmp_path, frontend):
    (tmp_path / "registration.json").write_text(
        json.dumps({"ids": {"source": "test-econ"}})
    )
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-test-token"
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content='data: {"object":"response","status":"completed"}\n\n',
            )
        return httpx.Response(200, json={"items": []})

    app = create_portal(
        tmp_path,
        "http://127.0.0.1:18767",
        "private-test-token",
        httpx.MockTransport(upstream),
        frontend=frontend,
    )
    headers = {"X-QwenPaw-Console": "1"}
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert "private-test-token" not in c.get("/assets/app.js").text
        assert c.get("/api/v1/sessions").status_code == 403
        assert (
            c.get(
                "/api/v1/sessions", headers={**headers, "Origin": "https://example.com"}
            ).status_code
            == 403
        )
        assert (
            c.get(
                "/api/v1/sessions", headers={**headers, "Host": "malicious.example"}
            ).status_code
            == 400
        )
        assert c.get("/api/v1/preferences", headers=headers).status_code == 404
        assert (
            c.get("/portal/config", headers=headers).json()["title"] == "QwenPaw Data"
        )
        assert c.get("/api/v1/sessions?page_size=20", headers=headers).json() == {
            "items": []
        }
        assert requests[-1].url.params["page_size"] == "20"
        r = c.get("/api/v1/sessions/s1/chats/c1/events", headers=headers)
        assert "data:" in r.text and r.headers["content-type"].startswith(
            "text/event-stream"
        )
        assert "private-test-token" not in r.text


def test_geo_routes_uploads_and_file_navigation(tmp_path, frontend):
    requests = []

    def upstream(request):
        requests.append(request)
        if request.url.path.endswith("/files/access"):
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html",
                    "content-disposition": 'inline; filename="report.html"',
                },
                content="<h1>Report</h1>",
            )
        return httpx.Response(200, json={})

    app = create_portal(
        tmp_path,
        "http://127.0.0.1:18767",
        "test-token",
        httpx.MockTransport(upstream),
        frontend=frontend,
    )
    headers = {"X-QwenPaw-Console": "1"}
    with TestClient(app) as c:
        for path in ("/console", "/console/sessions", "/console/settings/agent"):
            assert "GEO Console" in c.get(path).text
        assert c.get("/missing.js").status_code == 404
        for path in (
            "auth/status",
            "sessions/s1/snapshot",
            "sessions/s1/settlement/cards",
            "preferences/providers",
            "cron/jobs",
        ):
            assert c.get("/api/v1/" + path, headers=headers).status_code == 200
        for path in (
            "console/chat",
            "console/chat/stop",
            "sessions/s1/chats/c1/steer",
            "sessions/s1/chats/c1/clarification/answer",
        ):
            assert (
                c.post("/api/v1/" + path, headers=headers, json={}).status_code == 200
            )
        assert (
            c.post(
                "/api/v1/console/upload",
                headers=headers,
                files={"file": ("sample.csv", b"x,y")},
            ).status_code
            == 200
        )
        assert (
            requests[-1]
            .headers["content-type"]
            .startswith("multipart/form-data; boundary=")
        )
        url = "/api/v1/sessions/s1/files/access?path=report.html&purpose=preview"
        assert c.get(url).status_code == 403
        same_origin = {
            "Sec-Fetch-Site": "same-origin",
            "Referer": "http://testserver/console",
        }
        r = c.get(url, headers=same_origin)
        assert r.status_code == 200 and r.headers["content-disposition"].startswith(
            "inline"
        )
        assert (
            r.headers["content-security-policy"]
            == "sandbox allow-scripts allow-downloads"
        )
        assert (
            c.get(
                url, headers={**same_origin, "Referer": "https://example.com"}
            ).status_code
            == 403
        )
        assert c.get("/api/v1/skills/pool", headers=headers).status_code == 404
        assert c.get("/api/v1/mcp", headers=headers).status_code == 404


@pytest.fixture
def bridge_frontend(tmp_path):
    root = tmp_path / "bridge-ui"
    root.mkdir()
    (root / "index.html").write_text("<div>Official DataBridge</div>")
    (root / "assets").mkdir()
    (root / "assets/app.js").write_text("// official management UI")
    return root


def test_official_bridge_routes_auth_upload_and_download(
    tmp_path, frontend, bridge_frontend
):
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 18766
        assert request.headers["authorization"] == "Bearer private-test-token"
        if request.url.path.endswith("/download"):
            return httpx.Response(
                200,
                content=b"pdf bytes",
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="sample.pdf"',
                },
            )
        if request.url.path.endswith("/weave/events"):
            return httpx.Response(
                200,
                content="data: complete\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "download_url": "http://127.0.0.1:18766/api/v1/docs/d1/download",
            },
        )

    app = create_portal(
        tmp_path,
        "http://127.0.0.1:18767",
        "private-test-token",
        frontend=frontend,
        bridge_url="http://127.0.0.1:18766",
        bridge_frontend=bridge_frontend,
        bridge_transport=httpx.MockTransport(upstream),
    )
    headers = {"X-QwenPaw-Console": "1"}
    with TestClient(app) as client:
        for path in (
            "/databridge",
            "/databridge/",
            "/databridge/model-config",
            "/databridge/metric-lib",
        ):
            assert "Official DataBridge" in client.get(path).text
        assert client.get("/databridge/assets/app.js").status_code == 200
        assert client.get("/databridge/assets/missing.js").status_code == 404
        assert client.get("/databridge/api/auth/status").status_code == 403
        assert client.get("/databridge/api/auth/status", headers=headers).json() == {
            "required": False
        }
        assert requests[-1].url.path == "/api/auth/check"
        url = "/databridge/api/system/model-config/"
        assert (
            client.get(
                url, headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(url, headers={**headers, "Host": "evil.example"}).status_code
            == 400
        )
        assert client.get(url, headers=headers).status_code == 200
        assert requests[-1].url.path == "/api/system/model-config/"
        result = client.put(
            "/databridge/api/system/model-config/llm",
            headers={**headers, "Authorization": "Bearer browser-token"},
            json={"model": "test-model"},
        )
        assert result.status_code == 200
        assert json.loads(requests[-1].content) == {"model": "test-model"}
        result = client.get(
            "/databridge/api/semantic-config/datasource?page=2", headers=headers
        )
        assert requests[-1].url.params["page"] == "2"
        assert result.json()["download_url"] == "/databridge/api/v1/docs/d1/download"
        assert "private-test-token" not in result.text
        result = client.post(
            "/databridge/api/v1/docs/upload",
            headers=headers,
            files={"file": ("sample.pdf", b"PDF")},
        )
        assert result.status_code == 200
        assert (
            requests[-1]
            .headers["content-type"]
            .startswith("multipart/form-data; boundary=")
        )
        assert b"PDF" in requests[-1].content
        assert (
            "data: complete"
            in client.get("/databridge/api/weave/events", headers=headers).text
        )
        url = "/databridge/api/v1/docs/d1/download"
        assert client.get(url).status_code == 403
        nav = {
            "Sec-Fetch-Site": "same-origin",
            "Referer": "http://testserver/databridge/kg-docs",
        }
        assert client.get(url, headers=nav).content == b"pdf bytes"
        assert (
            client.get(
                url, headers={**nav, "Referer": "https://evil.example"}
            ).status_code
            == 403
        )
        count = len(requests)
        for path in (
            "/databridge/api/%2e%2e/private",
            "/databridge/api/foo//bar",
            "/databridge/api/foo%5cbar",
        ):
            assert client.get(path, headers=headers).status_code == 404
        assert len(requests) == count


def test_bridge_gate_checks_actual_upstream_auth(tmp_path, frontend, bridge_frontend):
    app = create_portal(
        tmp_path,
        "http://127.0.0.1:18767",
        "wrong-token",
        frontend=frontend,
        bridge_url="http://127.0.0.1:18766",
        bridge_frontend=bridge_frontend,
        bridge_transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"detail": "Unauthorized"})
        ),
    )
    with TestClient(app) as client:
        assert (
            client.get(
                "/databridge/api/auth/status", headers={"X-QwenPaw-Console": "1"}
            ).status_code
            == 502
        )


def test_bridge_configuration_requires_local_backend_and_built_ui(tmp_path, frontend):
    with pytest.raises(ValueError, match="loopback"):
        create_portal(
            tmp_path,
            "http://127.0.0.1:18767",
            "token",
            frontend=frontend,
            bridge_url="https://example.com",
        )
    with pytest.raises(ValueError, match="DataBridge assets missing"):
        create_portal(
            tmp_path,
            "http://127.0.0.1:18767",
            "token",
            frontend=frontend,
            bridge_url="http://127.0.0.1:18766",
            bridge_frontend=tmp_path / "absent",
        )


def test_runtime_branding_and_distinct_service_credentials(frontend, bridge_frontend):
    seen = []

    def backend(request):
        seen.append((request.url.port, request.headers["authorization"]))
        return httpx.Response(200, json={})

    config = ConsoleConfig(
        title="Research </script>", workspace_name="Research", sample_questions=["Why?"]
    )
    app = create_app(
        host_url="http://127.0.0.1:8766",
        token="host-only-secret",
        bridge_url="http://127.0.0.1:8765",
        bridge_token="bridge-only-secret",
        config=config,
        frontend=frontend,
        bridge_frontend=bridge_frontend,
        transport=httpx.MockTransport(backend),
        bridge_transport=httpx.MockTransport(backend),
    )
    with TestClient(app) as client:
        script = client.get("/console-config.js")
        assert script.headers["content-type"].startswith("application/javascript")
        assert "</script>" not in script.text
        assert (
            "host-only-secret" not in script.text
            and "bridge-only-secret" not in script.text
        )
        value = json.loads(script.text.split(" = ", 1)[1].removesuffix(";"))
        assert value["title"] == "Research </script>"
        assert value["sample_questions"] == ["Why?"]
        assert value["capabilities"]["contextConsole"] is True
        headers = {
            "X-QwenPaw-Console": "1",
            "Authorization": "Bearer malicious-browser-token",
        }
        client.get("/api/v1/datasources", headers=headers)
        client.get("/databridge/api/auth/status", headers=headers)
        assert seen == [
            (8766, "Bearer host-only-secret"),
            (8765, "Bearer bridge-only-secret"),
        ]


def test_resume_header_and_retry_after_survive_gateway(frontend):
    def backend(request):
        assert request.headers["last-event-id"] == "17"
        return httpx.Response(
            429, json={"detail": "retry"}, headers={"Retry-After": "3"}
        )

    app = create_app(
        host_url="http://127.0.0.1:8766",
        token="token",
        frontend=frontend,
        transport=httpx.MockTransport(backend),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/sessions/s1/chats/c1/events",
            headers={"X-QwenPaw-Console": "1", "Last-Event-ID": "17"},
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "3"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:pass@localhost:8766",
        "http://localhost:8766/private",
        "http://localhost:8766?token=x",
    ],
)
def test_upstream_must_be_fixed_loopback_origin(frontend, url):
    with pytest.raises(ValueError, match="loopback"):
        create_app(host_url=url, token="token", frontend=frontend)


def test_no_bridge_and_no_external_access(frontend):
    app = create_app(host_url="http://127.0.0.1:8766", token="token", frontend=frontend)
    with TestClient(app) as client:
        assert (
            client.get("/portal/config", headers={"X-QwenPaw-Console": "1"}).json()[
                "capabilities"
            ]["contextConsole"]
            is False
        )
        assert client.get("/databridge/").status_code == 404
    with TestClient(app, client=("192.0.2.10", 12345)) as client:
        assert client.get("/console").status_code == 403


@pytest.mark.parametrize("prefix", ["", "/databridge"])
def test_static_paths_cannot_escape_asset_root(tmp_path, frontend, bridge_frontend, prefix):
    secret = tmp_path / "secret.txt"
    secret.write_text("outside-asset-root")
    app = create_app(
        host_url="http://127.0.0.1:8766",
        token="token",
        frontend=frontend,
        bridge_url="http://127.0.0.1:8765",
        bridge_frontend=bridge_frontend,
    )
    with TestClient(app) as client:
        for path in (
            "/%2e%2e/secret.txt",
            "/assets/%2e%2e/%2e%2e/secret.txt",
            "/%2e%2e%5csecret.txt",
            "/%252e%252e/secret.txt",
            "/" + secret.as_posix().replace("/", "%2f"),
        ):
            response = client.get(prefix + path)
            assert response.status_code == 404, path
            assert "outside-asset-root" not in response.text
        assert client.get(prefix + "/assets/app.js").status_code == 200


@pytest.mark.parametrize("prefix", ["", "/databridge"])
def test_static_symlinks_cannot_expose_sibling_directory(
    tmp_path, frontend, bridge_frontend, prefix
):
    root = bridge_frontend if prefix else frontend
    # A shared string prefix does not make this a child of the assets directory.
    sibling = root.with_name(root.name + "-private")
    sibling.mkdir()
    (sibling / "secret.txt").write_text("outside-asset-root")
    try:
        (root / "assets/external").symlink_to(sibling, target_is_directory=True)
        (root / "secret.txt").symlink_to(sibling / "secret.txt")
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows runner does not permit symlink creation")
        raise
    app = create_app(
        host_url="http://127.0.0.1:8766",
        token="token",
        frontend=frontend,
        bridge_url="http://127.0.0.1:8765",
        bridge_frontend=bridge_frontend,
    )
    with TestClient(app) as client:
        for path in ("/assets/external/secret.txt", "/secret.txt"):
            response = client.get(prefix + path)
            assert response.status_code == 404
            assert "outside-asset-root" not in response.text
