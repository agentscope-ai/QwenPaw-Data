"""Native console admission carries bindings without persisting credentials."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.models.requests import ConsoleChatRequest
from qwenpaw_data.host.core.api.routers import console
from qwenpaw_data.host.core.domain.identity import Identity


@pytest.mark.parametrize("route", ["native", "compat"])
def test_native_models_and_dataset_context(tmp_path, monkeypatch, route):
    monkeypatch.setenv("QWENPAW_DATA_CONTEXT_API_TOKEN", "context-service-key")
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
    app = create_app()
    app.dependency_overrides[console.get_context_manager_client] = lambda: SimpleNamespace(
        list_datasets=lambda _: SimpleNamespace(
            items=[SimpleNamespace(dataset_name="daily_sales")]
        )
    )
    with TestClient(app) as http:
        caps = http.get("/api/v1/console/capabilities")
        assert caps.json()["runtime_model_bindings"] is True
        body["session_id"] = http.post("/api/v1/sessions", json={}).json()[
            "session"
        ]["id"]
        path = "/api/v1/console/chat" if route == "native" else (
            f"/api/v1/sessions/{body['session_id']}/chats"
        )
        payload = body if route == "native" else {
            key: value for key, value in body.items() if key != "session_id"
        }
        response = http.post(path, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["chat"]["dataset_names"] == ["daily_sales"]
        assert "private-per-turn-secret" not in response.text
        assert (
            run.call_args.kwargs["runtime_config"].default.model_id == "workspace-model"
        )
        header = run.call_args.kwargs["context_model_header"]
        assert json.loads(base64.urlsafe_b64decode(header))["api_key"] == binding["api_key"]
        serialized = ConsoleChatRequest.model_validate(body).model_dump_json()
        assert "private-per-turn-secret" not in serialized
        for file in tmp_path.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                assert binding["api_key"].encode() not in data
                assert header.encode() not in data
        app.dependency_overrides[get_identity] = lambda: Identity(
            user_id="another-user", tenant_id="another-tenant", workspace_id="another-workspace"
        )
        rejected = http.post(path, json=payload)
        assert rejected.status_code == 404
        assert run.call_count == 1


def test_native_console_rejects_disabled_clarification(tmp_path, monkeypatch):
    with TestClient(create_app()) as http:
        response = http.post(
            "/api/v1/console/chat",
            json={
                "session_id": "missing",
                "text": "hi",
                "enable_clarification": False,
            },
        )
        assert response.status_code == 422


@pytest.mark.parametrize("multi_user", [False, True])
def test_databridge_binding_rejects_missing_authority_before_admission(monkeypatch, multi_user):
    for name in ("QWENPAW_DATA_CONTEXT_API_TOKEN", "QWENPAW_DATA_CLIENT_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if multi_user:
        monkeypatch.setenv("QWENPAW_DATA_CONTEXT_API_TOKEN", "must-not-elevate")
    monkeypatch.setattr(console, "auth_enabled", lambda: multi_user)
    run = AsyncMock()
    monkeypatch.setattr(console.ChatRuntime, "run", run)
    with TestClient(create_app()) as http:
        sid = http.post("/api/v1/sessions", json={}).json()["session"]["id"]
        body = {
            "session_id": sid, "text": "hello",
            "runtime_models": {
                "analysis": {
                    "provider_id": "openai", "model_id": "turn-model",
                    "api_key": "private-turn-key", "base_url": "https://example.invalid/v1",
                },
                "inherit_analysis": ["databridge"],
            },
        }
        rejected = http.post("/api/v1/console/chat", json=body)
        assert rejected.status_code == 400, rejected.text
        assert "private-turn-key" not in rejected.text
        assert http.get(f"/api/v1/sessions/{sid}/chats").json()["items"] == []
        run.assert_not_awaited()
        # Analysis-only bindings need no DataBridge routing authority.
        body["runtime_models"]["inherit_analysis"] = []
        accepted = http.post("/api/v1/console/chat", json=body)
        assert accepted.status_code == 200, accepted.text
        assert run.call_args.kwargs["context_model_header"] is None
