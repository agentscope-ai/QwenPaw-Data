# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import reset_engine
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub


@pytest.fixture
def mcp_api(tmp_path, monkeypatch: pytest.MonkeyPatch):
    import asyncio

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as client:
        yield client


def _http_body(
    name: str = "search", url: str = "http://example.com/mcp"
) -> dict[str, Any]:
    return {
        "name": name,
        "description": "demo",
        "enabled": True,
        "source": "custom",
        "is_stateful": False,
        "mcp_config": {"type": "http_mcp", "url": url, "headers": {}, "timeout": 30},
        "enable_tools": None,
        "disable_tools": None,
        "execution_timeout": None,
    }


def test_list_seeds_builtin(mcp_api) -> None:
    client = mcp_api
    response = client.get("/api/mcp")
    assert response.status_code == 200, response.text
    names = [item["name"] for item in response.json()["mcps"]]
    assert "context-manager" in names
    builtin = next(
        item for item in response.json()["mcps"] if item["name"] == "context-manager"
    )
    assert builtin["source"] == "builtin"
    assert builtin["mcp_config"]["url"] == get_settings().cm.mcp_url()
    assert builtin["disable_tools"] == [
        "execute_create",
        "execute_insert",
        "execute_update",
        "execute_modify",
    ]


def test_update_builtin_tools(mcp_api) -> None:
    client = mcp_api
    updated = client.put(
        "/api/mcp/context-manager/tools",
        json={"enable_tools": None, "disable_tools": ["execute_insert"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["source"] == "builtin"
    assert updated.json()["enable_tools"] is None
    assert updated.json()["disable_tools"] == ["execute_insert"]


def test_create_update_enable_delete(mcp_api) -> None:
    client = mcp_api
    created = client.post("/api/mcp", json=_http_body())
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "search"
    assert created.json()["mcp_config"]["url"] == "http://example.com/mcp"

    listed = client.get("/api/mcp")
    assert any(item["name"] == "search" for item in listed.json()["mcps"])

    updated = client.put(
        "/api/mcp/search",
        json=_http_body(url="http://other.example/mcp"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["mcp_config"]["url"] == "http://other.example/mcp"

    disabled = client.post("/api/mcp/search/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    enabled = client.post("/api/mcp/search/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    deleted = client.delete("/api/mcp/search")
    assert deleted.status_code == 200
    names = [item["name"] for item in client.get("/api/mcp").json()["mcps"]]
    assert "search" not in names


def test_update_name_mismatch(mcp_api) -> None:
    client = mcp_api
    client.post("/api/mcp", json=_http_body())
    response = client.put("/api/mcp/search", json=_http_body(name="other"))
    assert response.status_code == 400, response.text


def test_create_conflict(mcp_api) -> None:
    client = mcp_api
    assert client.post("/api/mcp", json=_http_body()).status_code == 200
    response = client.post("/api/mcp", json=_http_body())
    assert response.status_code == 409, response.text


def test_list_tools(mcp_api, monkeypatch: pytest.MonkeyPatch) -> None:
    client = mcp_api
    assert client.post("/api/mcp", json=_http_body()).status_code == 200

    async def fake_list_tools(self, name: str, *, access_token: str | None = None):
        _ = access_token
        mcp = self.require(name)
        tool = SimpleNamespace(
            name="ping",
            description="pong",
            inputSchema={"type": "object", "properties": {}},
        )
        return mcp, [tool]

    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.routers.mcp.McpRepository.list_tools",
        fake_list_tools,
    )
    response = client.get("/api/mcp/search/tools")
    assert response.status_code == 200, response.text
    tools = response.json()["tools"]
    assert tools[0]["name"] == "ping"
    assert tools[0]["enabled"] is True

    updated = client.put("/api/mcp/search/tools", json={"enable_tools": ["ping"]})
    assert updated.status_code == 200
    assert updated.json()["enable_tools"] == ["ping"]
    assert updated.json()["disable_tools"] is None

    denied = client.put(
        "/api/mcp/search/tools",
        json={"enable_tools": None, "disable_tools": ["ping"]},
    )
    assert denied.status_code == 200
    assert denied.json()["enable_tools"] is None
    assert denied.json()["disable_tools"] == ["ping"]
