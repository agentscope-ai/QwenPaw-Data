# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import reset_engine
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import asyncio

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QWENPAW_DATA_CHANNELS", "dingtalk")
    monkeypatch.setenv("QWENPAW_DATA_PROVIDERS", "dashscope")
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_channel_config_lists_only_allowlisted(client: TestClient) -> None:
    response = client.get("/api/system/channel-config/")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"dingtalk"}


def test_channel_config_rejects_hidden_channel(client: TestClient) -> None:
    response = client.put("/api/system/channel-config/feishu", json={"enabled": True})
    assert response.status_code == 404, response.text


def test_providers_list_only_allowlisted(client: TestClient) -> None:
    response = client.get("/api/preferences/providers")
    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()["providers"]} == {"dashscope"}
