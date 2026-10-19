# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.core import QwenPawDataHost  # noqa: E402
from qwenpaw_data.host.core.providers.registry import ActiveModel  # noqa: E402


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, **app_kwargs):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_PREFS_MASTER_SECRET", raising=False)
    app = create_app(home=tmp_path, **app_kwargs)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


async def test_provider_config_roundtrip(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch, model=object()) as (http, _):
        listing = await http.get("/api/v1/preferences/providers")
        providers = {p["id"]: p for p in listing.json()["providers"]}
        assert set(providers) == {"dashscope", "openai"}
        assert not providers["dashscope"]["configured"]

        saved = await http.put(
            "/api/v1/preferences/providers/dashscope",
            json={"api_key": "sk-super-secret"},
        )
        assert saved.status_code == 200
        provider = saved.json()["provider"]
        assert provider["configured"] is True
        assert provider["api_key_masked"] == "sk******"
        assert "sk-super-secret" not in saved.text

        missing_key = await http.put(
            "/api/v1/preferences/providers/openai", json={"base_url": "https://x"}
        )
        assert missing_key.status_code == 400

        unknown = await http.put(
            "/api/v1/preferences/providers/nope", json={"api_key": "k"}
        )
        assert unknown.status_code == 400

        cleared = await http.delete("/api/v1/preferences/providers/dashscope")
        assert cleared.status_code == 200
        assert (
            await http.delete("/api/v1/preferences/providers/dashscope")
        ).status_code == 404


async def test_analysis_readiness_uses_local_agent_settings_not_caller_prefs(
    tmp_path, monkeypatch,
) -> None:
    built = []

    def no_env_model():
        raise RuntimeError("sensitive configuration detail")

    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.app.build_model_from_env", no_env_model,
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.app.build_model",
        lambda active: built.append(active) or object(),
    )
    async with service_client(tmp_path, monkeypatch) as (http, _):
        url = "/api/v1/capabilities/analysis"
        missing = await http.get(url, headers={"X-User-Id": "pawapp-scoped"})
        assert missing.json() == {"readiness_version": 1, "model_configured": False}
        assert "sensitive" not in missing.text
        await http.put(
            "/api/v1/preferences/providers/dashscope",
            json={"api_key": "private-agent-key"},
        )
        await http.put(
            "/api/v1/preferences/active-models",
            json={"default_provider_id": "dashscope", "default_model_id": "qwen-max"},
        )
        ready = await http.get(url, headers={"X-User-Id": "pawapp-scoped"})
        assert ready.json() == {"readiness_version": 1, "model_configured": True}
        assert built[-1].api_key == "private-agent-key"
        assert "private-agent-key" not in ready.text


async def test_analysis_readiness_explicit_model_and_env_fallback(tmp_path, monkeypatch):
    async with service_client(tmp_path / "explicit", monkeypatch, model=object()) as (http, _):
        assert (await http.get("/api/v1/capabilities/analysis")).json()["model_configured"]
    calls = []
    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.app.build_model_from_env",
        lambda: calls.append(True) or object(),
    )
    async with service_client(tmp_path / "env", monkeypatch) as (http, _):
        assert (await http.get("/api/v1/capabilities/analysis")).json()["model_configured"]
        assert calls == [True]


async def test_models_and_active_selection(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch, model=object()) as (http, _):
        added = await http.put(
            "/api/v1/preferences/providers/dashscope/models/my-model",
            json={"source": "extra", "name": "My Model"},
        )
        assert added.status_code == 200
        assert added.json()["model"]["source"] == "extra"

        models = await http.get("/api/v1/preferences/providers/dashscope/models")
        ids = {m["id"] for m in models.json()["models"]}
        assert "my-model" in ids and "qwen-max" in ids

        # active selection blocked until credentials exist
        blocked = await http.put(
            "/api/v1/preferences/active-models",
            json={
                "default_provider_id": "dashscope",
                "default_model_id": "my-model",
            },
        )
        assert blocked.status_code == 400

        await http.put(
            "/api/v1/preferences/providers/dashscope", json={"api_key": "sk-k"}
        )
        ok = await http.put(
            "/api/v1/preferences/active-models",
            json={
                "default_provider_id": "dashscope",
                "default_model_id": "my-model",
            },
        )
        assert ok.status_code == 200
        active = await http.get("/api/v1/preferences/active-models")
        assert active.json()["active_models"]["default_model_id"] == "my-model"


async def test_host_model_resolves_from_preferences(tmp_path, monkeypatch) -> None:
    """With no explicit model, a new host resolves prefs → ActiveModel."""
    built: list[ActiveModel] = []

    def fake_build_model(active: ActiveModel) -> object:
        built.append(active)
        return object()

    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.app.build_model",
        fake_build_model,
    )

    async with service_client(tmp_path, monkeypatch) as (http, app):
        await http.put(
            "/api/v1/preferences/providers/dashscope", json={"api_key": "sk-k"}
        )
        await http.put(
            "/api/v1/preferences/active-models",
            json={
                "default_provider_id": "dashscope",
                "default_model_id": "qwen-max",
            },
        )

        hosts = app.state.service.hosts
        host = hosts.get(session_id="ses-model-test")
        assert isinstance(host, QwenPawDataHost)
        assert host.model is None  # deferred until first agent build

        resolved = await host._model_factory()
        assert built and built[0].model_id == "qwen-max"
        assert built[0].api_key == "sk-k"
        assert resolved is not None
