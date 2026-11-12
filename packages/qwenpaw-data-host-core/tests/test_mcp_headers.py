# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

_FORMATTING = "qwenpaw_data.host.core.algo.biztrace.formatting"
try:
    __import__(_FORMATTING)
except ModuleNotFoundError:
    _stub = types.ModuleType(_FORMATTING)
    _stub.FILE_PATH_KEYS = frozenset()
    _stub.file_basename = lambda path: str(path).rsplit("/", 1)[-1]
    _stub.first_str = lambda *args, **kwargs: ""
    _stub.is_business_file = lambda path: False
    _stub.output_text = lambda *args, **kwargs: ""
    sys.modules[_FORMATTING] = _stub

from qwenpaw_data.host.core.settings.mcp import McpSettings  # noqa: E402
from qwenpaw_data.host.core.utils.mcp_headers import (  # noqa: E402
    merge_mcp_headers,
    parse_mcp_headers,
)


def test_parse_mcp_headers_empty() -> None:
    assert parse_mcp_headers(None) == {}
    assert parse_mcp_headers("") == {}
    assert parse_mcp_headers("   ") == {}


def test_parse_mcp_headers_object() -> None:
    assert parse_mcp_headers(
        '{"context-manager":{"X-Account-Site":"cn"},'
        '"aliyun-openapi":{"Authorization":"Bearer tok"}}'
    ) == {
        "context-manager": {"X-Account-Site": "cn"},
        "aliyun-openapi": {"Authorization": "Bearer tok"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '"tok"',
        "{",
        '{"context-manager":"cn"}',
        '{"":{"X-Account-Site":"cn"}}',
        '{"context-manager":{"X-Account-Site":1}}',
    ],
)
def test_parse_mcp_headers_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match=McpSettings.HEADERS_HEADER):
        parse_mcp_headers(raw)


def test_merge_mcp_headers_overlay_wins_per_header() -> None:
    # The differently-cased name is the same header, so the overlay replaces
    # it instead of arriving beside it (httpx comma-joins case variants).
    base = {"context-manager": {"X-DataAgent-Service-Id": "base", "X-Keep": "b"}}
    overlay = {
        "context-manager": {"x-dataagent-service-id": "overlay"},
        "search": {"X-Custom": "overlay"},
    }
    assert merge_mcp_headers(base, overlay) == {
        "context-manager": {"x-dataagent-service-id": "overlay", "x-keep": "b"},
        "search": {"x-custom": "overlay"},
    }


def test_configured_headers_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env name is the natural pydantic-settings binding for this field.
    monkeypatch.setenv(
        "QWENPAW_DATA_MCP_REGISTRATION_HEADERS",
        '{"context-manager":{"X-DataAgent-Service-Id":"svc-123",'
        '"X-DataAgent-Uid":"123456"}}',
    )
    assert McpSettings().registration_headers == {
        "context-manager": {
            "X-DataAgent-Service-Id": "svc-123",
            "X-DataAgent-Uid": "123456",
        }
    }


def test_configured_headers_reject_names_only_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 2026-09 shape listed header names only and took the values from the
    # request, so a run without one could never use it. Fail loudly instead.
    monkeypatch.setenv(
        "QWENPAW_DATA_MCP_REGISTRATION_HEADERS",
        '{"context-manager":["X-DataAgent-Service-Id"]}',
    )
    with pytest.raises(ValidationError):
        McpSettings()


@pytest.fixture
def chat_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
):
    from fastapi.testclient import TestClient

    from qwenpaw_data.host.core.api.app import create_app
    from qwenpaw_data.host.core.api.routers import console as console_router
    from qwenpaw_data.host.core.db.engine import get_session_factory, reset_engine
    from qwenpaw_data.host.core.settings import clear_settings
    from qwenpaw_data.host.core.stream.hub import reset_hub

    captured: dict[str, Any] = {}

    async def _run(
        self,
        *,
        identity: Any,
        access_token: str | None = None,
        mcp_headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        captured["mcp_headers"] = mcp_headers

    asyncio.run(reset_engine())
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    monkeypatch.setattr(console_router.ChatRuntime, "run", _run)
    app = create_app()
    with TestClient(app) as client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        yield client, captured


def test_console_chat_forwards_mcp_headers(chat_client) -> None:
    client, captured = chat_client
    session_id = client.post("/api/sessions", json={"title": "mcp"}).json()["session"][
        "id"
    ]
    overlay = {
        "context-manager": {"X-Account-Site": "cn"},
        "aliyun-openapi": {"Authorization": "Bearer tok"},
    }
    created = client.post(
        "/api/console/chat",
        json={
            "session_id": session_id,
            "text": "hello",
            "datasource_id": "ds_demo",
        },
        headers={McpSettings.HEADERS_HEADER: json.dumps(overlay)},
    )
    assert created.status_code == 200, created.text
    assert captured["mcp_headers"] == overlay


def test_console_chat_rejects_invalid_mcp_headers(chat_client) -> None:
    client, captured = chat_client
    session_id = client.post("/api/sessions", json={"title": "bad"}).json()["session"][
        "id"
    ]
    created = client.post(
        "/api/console/chat",
        json={
            "session_id": session_id,
            "text": "hello",
            "datasource_id": "ds_demo",
        },
        headers={McpSettings.HEADERS_HEADER: "{"},
    )
    assert created.status_code == 400, created.text
    assert created.json()["code"] == "VALIDATION"
    assert "mcp_headers" not in captured
