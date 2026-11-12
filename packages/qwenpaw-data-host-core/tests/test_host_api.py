# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.runtime.builder import DataAgentBuilder
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def _stub_cm_mcp_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The builtin context-manager MCP is an external service.

    Chat turns now fail fast when the CM MCP cannot be reached, so unit tests
    that only care about the Host-side turn must stub the network probe.
    """
    monkeypatch.setattr(McpRepository, "sync", AsyncMock())


class FakeTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "fake-host-api-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, messages, tools, tool_choice, kwargs)
        return ChatResponse(
            content=[TextBlock(text="hello")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def test_session_chat_events_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    import asyncio

    asyncio.run(reset_engine())
    reset_hub()
    fake_model = FakeTextModel()
    home = tmp_path / "home"
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    clear_settings()
    _stub_cm_mcp_sync(monkeypatch)
    monkeypatch.setattr(
        builder_module,
        "build_model",
        lambda *_args, **_kwargs: fake_model,
    )
    app = create_app()
    headers = {}

    with TestClient(app) as client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        r = client.post("/api/sessions", json={"title": "t1"}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["session"]["agent_id"] == "default"

        r = client.post(
            "/api/sessions",
            json={"title": "agent session", "agent_id": "agent-a"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        session = r.json()["session"]
        assert session["agent_id"] == "agent-a"
        session_id = session["id"]

        r = client.get(f"/api/sessions/{session_id}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["session"]["agent_id"] == "agent-a"

        r = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_id": "agent-b"},
            headers=headers,
        )
        assert r.status_code == 422, r.text

        r = client.post(
            "/api/sessions",
            json={"title": "invalid agent", "agent_id": "../escape"},
            headers=headers,
        )
        assert r.status_code == 400, r.text

        r = client.post(
            "/api/console/chat",
            json={
                "session_id": session_id,
                "text": "hello",
                "datasource_id": "ds_demo",
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        chat = r.json()["chat"]
        assert chat["status"] == "running"
        assert chat["last_sequence_number"] == -1
        chat_id = chat["id"]

        deadline = time.time() + 5
        last = -1
        while time.time() < deadline:
            snap = client.get(f"/api/sessions/{session_id}/snapshot", headers=headers)
            assert snap.status_code == 200
            c0 = snap.json()["chats"][0]
            last = c0["last_sequence_number"]
            if c0["status"] == "completed" and last >= 0:
                break
            time.sleep(0.05)
        assert last >= 0

        with client.stream(
            "GET",
            f"/api/sessions/{session_id}/chats/{chat_id}/events",
            params={"after_sequence_number": -1},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert "event: response" in body
        assert "event: message" in body
        assert '"status":"completed"' in body.replace(" ", "")

        data_lines = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ")]
        assert data_lines
        first = json.loads(data_lines[0])
        assert first["sequence_number"] == 0
        assert first["chat_id"] == chat_id
        expected_workspace = (
            home
            / TEST_IDENTITY.tenant_id
            / TEST_IDENTITY.workspace_id
            / TEST_IDENTITY.user_id
            / "agent-a"
            / "workspace"
        )
        assert expected_workspace.is_dir()
        assert (expected_workspace / "artifacts" / session_id).is_dir()
        assert not (home / "sessions").exists()


def test_agent_build_failure_marks_chat_failed_and_closes_stream(
    tmp_path: Path,
    monkeypatch,
    seed_test_preferences,
) -> None:
    import asyncio

    async def fail_build(*args, **kwargs):
        _ = (args, kwargs)
        raise RuntimeError("model configuration missing")

    monkeypatch.setattr(DataAgentBuilder, "build", fail_build)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    _stub_cm_mcp_sync(monkeypatch)
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    headers = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        session_response = client.post(
            "/api/sessions",
            json={"title": "failure"},
            headers=headers,
        )
        session_id = session_response.json()["session"]["id"]
        chat_response = client.post(
            "/api/console/chat",
            json={
                "session_id": session_id,
                "text": "hello",
                "datasource_id": "ds_demo",
            },
            headers=headers,
        )
        assert chat_response.status_code == 200, chat_response.text
        chat_id = chat_response.json()["chat"]["id"]

        snapshot = client.get(
            f"/api/sessions/{session_id}/snapshot",
            headers=headers,
        ).json()
        chat = snapshot["chats"][0]
        assert chat["status"] == "failed"
        assert chat["last_sequence_number"] >= 2
        # The client-facing message is the localized generic one; the raw
        # reason is carried in the failure cause.
        assert chat["error"]["code"] == "INTERNAL"
        assert chat["error"]["message"] == "执行失败，请稍后重试。"
        assert chat["error"]["details"]["cause"] == "model configuration missing"

        with client.stream(
            "GET",
            f"/api/sessions/{session_id}/chats/{chat_id}/events",
            headers=headers,
        ) as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert '"status":"failed"' in body.replace(" ", "")


def test_workspace_file_access_preview_download_and_path_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import asyncio

    home = tmp_path / "qwenpaw-data-home"
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    headers = {}

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions",
            json={"title": "files"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        session = response.json()["session"]
        session_id = session["id"]
        paths = UserAgentPaths.from_identity(
            home=home,
            identity=TEST_IDENTITY,
            agent_id=session["agent_id"],
            session_id=session_id,
        )
        result_file = paths.artifact_dir / "analysis" / "result.csv"
        result_file.parent.mkdir(parents=True)
        result_file.write_text("day,dau\n2026-07-30,42\n", encoding="utf-8")

        preview = client.get(
            f"/api/sessions/{session_id}/files/access",
            params={"path": "analysis/result.csv", "purpose": "preview"},
            headers=headers,
        )
        assert preview.status_code == 200
        assert preview.text.startswith("day,dau")
        assert preview.headers["content-disposition"].startswith("inline;")

        download = client.get(
            f"/api/sessions/{session_id}/files/access",
            params={"path": "analysis/result.csv", "purpose": "download"},
            headers=headers,
        )
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")

        report = result_file.with_name("report.html")
        html = "<html><head></head><body>报告<script>echarts.init(dom)</script></body></html>"
        # Preserve original bytes, including BOM, CRLF and legacy encodings.
        for content in (
            html.encode("utf-8"),
            (html + "\r\n").encode("utf-8-sig"),
            html.encode("gbk"),
        ):
            report.write_bytes(content)
            for purpose in ("preview", "download"):
                response = client.get(
                    f"/api/sessions/{session_id}/files/access",
                    params={"path": "analysis/report.html", "purpose": purpose},
                )
                assert response.status_code == 200
                assert response.content == content
                assert int(response.headers["content-length"]) == len(content)
                assert response.headers["content-type"].startswith("text/html")
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert response.headers["x-content-type-options"] == "nosniff"
                csp = response.headers["content-security-policy"]
                assert "sandbox allow-scripts;" in csp
                assert "allow-same-origin" not in csp
                assert "script-src 'unsafe-inline' https:;" in csp
                assert "connect-src 'none'" in csp
                assert "strict-dynamic" not in csp
                assert response.headers["content-disposition"].startswith(
                    "inline;" if purpose == "preview" else "attachment;"
                )
            assert report.read_bytes() == content

        traversal = client.get(
            f"/api/sessions/{session_id}/files/access",
            params={"path": "../secret.txt", "purpose": "preview"},
            headers=headers,
        )
        assert traversal.status_code == 400
        assert traversal.json()["code"] == "VALIDATION"

        missing = client.get(
            f"/api/sessions/{session_id}/files/access",
            params={"path": "analysis/missing.csv", "purpose": "preview"},
            headers=headers,
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "NOT_FOUND"


def test_create_review_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import asyncio

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    headers = {}

    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            json={"title": "月报"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        parent = created.json()["session"]
        parent_id = parent["id"]
        assert parent["agent_id"] == "default"

        missing = client.post(
            "/api/sessions",
            json={
                "agent_id": "review",
                "title": "审核：月报",
                "parent_session_id": "ses_missing",
            },
            headers=headers,
        )
        assert missing.status_code == 404, missing.text

        reviewed = client.post(
            "/api/sessions",
            json={
                "agent_id": "review",
                "title": "审核：月报",
                "parent_session_id": parent_id,
            },
            headers=headers,
        )
        assert reviewed.status_code == 200, reviewed.text
        review_session = reviewed.json()["session"]
        assert review_session["agent_id"] == "review"
        assert review_session["parent_session_id"] == parent_id
        assert review_session["title"] == "审核：月报"
        assert review_session["forked_from_chat_id"] is None
        assert review_session["datasource_id"] is None
