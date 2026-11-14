# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import reset_engine
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import asyncio

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {}


def test_provider_active_model_and_runtime_roundtrip(client: TestClient) -> None:
    headers = _headers()

    r = client.get("/api/preferences/providers", headers=headers)
    assert r.status_code == 200, r.text
    providers = r.json()["providers"]
    assert {item["id"] for item in providers} >= {"dashscope", "openai"}
    dashscope = next(item for item in providers if item["id"] == "dashscope")
    assert dashscope["configured"] is False

    r = client.put(
        "/api/preferences/providers/dashscope",
        json={"api_key": "sk-test-key"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    provider = r.json()["provider"]
    assert provider["configured"] is True
    assert provider["api_key_masked"].endswith("******")
    assert "sk-test-key" not in r.text

    r = client.put(
        "/api/preferences/providers/dashscope/models/custom-model",
        json={"source": "extra", "name": "Custom Model"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"]["source"] == "extra"

    r = client.put(
        "/api/preferences/providers/dashscope/models/qwen-plus",
        json={"source": "override", "thinking_enabled": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"]["source"] == "override"
    assert r.json()["model"]["thinking_enabled"] is True

    r = client.put(
        "/api/preferences/active-models",
        json={
            "default_provider_id": "dashscope",
            "default_model_id": "qwen-plus",
            "light_provider_id": "dashscope",
            "light_model_id": "qwen3.7-flash",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    active = r.json()["active_models"]
    assert active["default_model_id"] == "qwen-plus"
    assert active["light_model_id"] == "qwen3.7-flash"

    r = client.get("/api/preferences/runtime-settings", headers=headers)
    assert r.status_code == 200, r.text
    assert (
        r.json()["runtime_settings"]["tool_timeout_seconds"]
        == get_settings().runtime.tool_timeout_seconds
    )
    assert r.json()["runtime_settings"]["tool_timeout_enabled"] is True
    assert r.json()["runtime_settings"]["tool_confirmation_level"] == "auto"

    r = client.put(
        "/api/preferences/runtime-settings",
        json={
            "react_max_iters": 12,
            "tool_timeout_enabled": False,
            "tool_timeout_seconds": 30,
            "tool_confirmation_level": "all",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    runtime = r.json()["runtime_settings"]
    assert runtime["react_max_iters"] == 12
    assert runtime["llm_retry_enabled"] is True
    assert runtime["llm_max_retries"] == 3
    assert runtime["tool_timeout_enabled"] is False
    assert runtime["tool_timeout_seconds"] == 30
    assert runtime["tool_confirmation_level"] == "all"

    r = client.put(
        "/api/preferences/runtime-settings",
        json={"tool_timeout_seconds": 0},
        headers=headers,
    )
    assert r.status_code == 422, r.text


def test_chat_requires_configured_preferences(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_FALLBACK_ENABLED", "false")
    clear_settings()
    headers = _headers()
    r = client.post("/api/sessions", json={"title": "needs model"}, headers=headers)
    assert r.status_code == 200, r.text
    session_id = r.json()["session"]["id"]

    r = client.post(
        "/api/console/chat",
        json={"session_id": session_id, "text": "hi", "datasource_id": "ds"},
        headers=headers,
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "VALIDATION"


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        (None, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("  ", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        (" https://models.example.test/v1 ", "https://models.example.test/v1"),
    ],
)
def test_resolve_uses_fallback_when_user_prefs_missing(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str | None,
    expected_url: str,
) -> None:
    from qwenpaw_data.host.core.domain.preference import UserPreferences

    monkeypatch.setenv("QWENPAW_DATA_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_PROVIDER", "dashscope")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_NAME", "qwen-plus")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_API_KEY", "sk-fallback")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_LIGHT_NAME", "qwen3.7-flash")
    if base_url is None:
        monkeypatch.delenv("QWENPAW_DATA_MODEL_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("QWENPAW_DATA_MODEL_BASE_URL", base_url)
    clear_settings()

    config = UserPreferences(user_id="u1").resolve()
    assert config.default.provider_id == "dashscope"
    assert config.default.model_id == "qwen-plus"
    assert config.default.api_key == "sk-fallback"
    assert config.default.base_url == expected_url
    assert config.default.thinking_enabled is None
    assert config.light is not None
    assert config.light.model_id == "qwen3.7-flash"
    assert config.light.api_key == "sk-fallback"
    assert config.light.base_url == expected_url


def test_resolve_prefers_user_provider_url_over_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw_data.host.core.domain.preference import ProviderCredential, UserPreferences

    monkeypatch.setenv("QWENPAW_DATA_MODEL_BASE_URL", "https://fallback.example.test/v1")
    clear_settings()

    config = UserPreferences(
        user_id="u1",
        providers={
            "dashscope": ProviderCredential(
                api_key="sk-test", base_url="https://user.example.test/v1"
            )
        },
        default_provider_id="dashscope",
        default_model_id="qwen-plus",
    ).resolve()

    assert config.default.base_url == "https://user.example.test/v1"


def test_resolve_carries_thinking_enabled() -> None:
    from qwenpaw_data.host.core.domain.preference import (
        ModelOverride,
        ProviderCredential,
        UserPreferences,
    )

    config = UserPreferences(
        user_id="u1",
        providers={"dashscope": ProviderCredential(api_key="sk-test", base_url=None)},
        models={
            ("dashscope", "qwen3.8-max"): ModelOverride(
                source="override",
                name=None,
                thinking_enabled=True,
            )
        },
        default_provider_id="dashscope",
        default_model_id="qwen3.8-max",
    ).resolve()

    assert config.default.model_id == "qwen3.8-max"
    assert config.default.thinking_enabled is True
    assert config.tool_timeout_enabled is True
    assert config.tool_timeout_seconds == get_settings().runtime.tool_timeout_seconds
    assert config.tool_confirmation_level == "auto"


def test_resolve_uses_user_tool_timeout() -> None:
    from qwenpaw_data.host.core.domain.preference import (
        ProviderCredential,
        UserPreferences,
    )

    config = UserPreferences(
        user_id="u1",
        providers={"dashscope": ProviderCredential(api_key="sk-test", base_url=None)},
        default_provider_id="dashscope",
        default_model_id="qwen-plus",
        tool_timeout_enabled=False,
        tool_timeout_seconds=30,
        tool_confirmation_level="off",
    ).resolve()
    assert config.tool_timeout_enabled is False
    assert config.tool_timeout_seconds == 30
    assert config.tool_confirmation_level == "off"


def test_chat_allows_fallback_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QWENPAW_DATA_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_PROVIDER", "dashscope")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_NAME", "qwen-plus")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_API_KEY", "sk-fallback")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_LIGHT_NAME", "")
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as client:
        headers = _headers()
        r = client.post("/api/sessions", json={"title": "fallback"}, headers=headers)
        assert r.status_code == 200, r.text
        session_id = r.json()["session"]["id"]

        r = client.post(
            "/api/console/chat",
            json={"session_id": session_id, "text": "hi", "datasource_id": "ds"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["chat"]["status"] == "running"
