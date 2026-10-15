"""Native console admission carries bindings without persisting credentials."""

import asyncio
from unittest.mock import AsyncMock

from test_sessions_api import service_client
from qwenpaw_data.host.core.api.models.requests import ConsoleChatRequest
from qwenpaw_data.host.core.api.routers import console


async def test_native_models_and_dataset_context(tmp_path, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(console.ChatRuntime, "run", run)
    binding = {
        "provider_id": "openai",
        "model_id": "workspace-model",
        "api_key": "private-per-turn-secret",
        "base_url": "https://example.invalid/v1",
    }
    body = {
        "text": "daily sales",
        "datasource_id": "sales-db",
        "dataset_names": ["daily_sales", "daily_sales"],
        "runtime_models": {
            "analysis": binding,
            "inherit_analysis": ["lightweight", "databridge"],
        },
    }
    async with service_client(tmp_path, monkeypatch) as http:
        http.headers["X-User-Id"] = "workspace-owner"
        caps = await http.get("/api/v1/console/capabilities")
        assert caps.json()["runtime_model_bindings"] is True
        body["session_id"] = (await http.post("/api/v1/sessions", json={})).json()[
            "session"
        ]["id"]
        response = await http.post("/api/v1/console/chat", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["chat"]["dataset_names"] == ["daily_sales"]
        assert "private-per-turn-secret" not in response.text
        await asyncio.sleep(0)
        assert (
            run.call_args.kwargs["runtime_config"].default.model_id == "workspace-model"
        )
        assert run.call_args.kwargs["context_model_header"]
        serialized = ConsoleChatRequest.model_validate(body).model_dump_json()
        assert "private-per-turn-secret" not in serialized
        for path in tmp_path.rglob("*.json"):
            assert "private-per-turn-secret" not in path.read_text()
        http.headers["X-User-Id"] = "another-workspace"
        rejected = await http.post("/api/v1/console/chat", json=body)
        assert rejected.status_code == 404
        assert run.call_count == 1


async def test_native_console_rejects_disabled_clarification(tmp_path, monkeypatch):
    async with service_client(tmp_path, monkeypatch) as http:
        response = await http.post(
            "/api/v1/console/chat",
            json={
                "session_id": "missing",
                "text": "hi",
                "enable_clarification": False,
            },
        )
        assert response.status_code == 422
