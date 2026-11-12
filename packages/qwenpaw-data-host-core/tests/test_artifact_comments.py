# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import get_session_factory, reset_engine
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

COMMENT = {
    "path": "russia_dau_mom.sql",
    "line_start": 2,
    "line_end": 10,
    "comment": "加一个参数",
}


class FakeTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "fake-artifact-comment-model"
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
            content=[TextBlock(text="ok")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
):
    asyncio.run(reset_engine())
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    monkeypatch.setattr(
        builder_module,
        "build_model",
        lambda *_args, **_kwargs: FakeTextModel(),
    )
    app = create_app()
    with TestClient(app) as test_client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        yield test_client


def test_console_chat_persists_artifact_comments(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={"title": "comments"}).json()[
        "session"
    ]["id"]

    created = client.post(
        "/api/console/chat",
        json={
            "session_id": session_id,
            "text": "关于 russia_dau_mom.sql L2–L10：\n加一个参数",
            "datasource_id": "ds_demo",
            "artifact_comments": [COMMENT],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["chat"]["artifact_comments"] == [COMMENT]
    assert created.json()["chat"]["user_input"].startswith("关于")

    snapshot = client.get(f"/api/sessions/{session_id}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["chats"][0]["artifact_comments"] == [COMMENT]


def test_console_chat_defaults_artifact_comments_empty(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={"title": "plain"}).json()[
        "session"
    ]["id"]
    created = client.post(
        "/api/console/chat",
        json={
            "session_id": session_id,
            "text": "hello",
            "datasource_id": "ds_demo",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["chat"]["artifact_comments"] == []
