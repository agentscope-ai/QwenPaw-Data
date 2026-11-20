"""Provider API persistence and per-run model assembly on the current service."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from agentscope.state import AgentState
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.model_usage import ModelUsageSource
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import PreferencesRepository
from qwenpaw_data.host.core.runtime.builder import data_agent
from qwenpaw_data.host.core.runtime.context import RunContext

from . import test_runtime_settings
from .conftest import TEST_IDENTITY
from .test_qwenpaw_data_agent import FakeTextModel

service = test_runtime_settings.service


async def test_provider_config_roundtrip(service) -> None:
    listing = await service.get("/api/preferences/providers")
    assert listing.status_code == 200, listing.text
    providers = {p["id"]: p for p in listing.json()["providers"]}
    assert set(providers) == {"dashscope", "openai"}
    assert not providers["dashscope"]["configured"]

    saved = await service.put(
        "/api/preferences/providers/dashscope",
        json={"api_key": "sk-super-secret"},
    )
    assert saved.status_code == 200, saved.text
    provider = saved.json()["provider"]
    assert provider["configured"] is True
    assert provider["api_key_masked"] == "sk******"
    assert "sk-super-secret" not in saved.text
    listing = await service.get("/api/preferences/providers")
    assert "sk-super-secret" not in listing.text

    missing_key = await service.put(
        "/api/preferences/providers/openai",
        json={"base_url": "https://x"},
    )
    assert missing_key.status_code == 400
    unknown = await service.put(
        "/api/preferences/providers/nope", json={"api_key": "k"}
    )
    assert unknown.status_code == 400

    cleared = await service.delete("/api/preferences/providers/dashscope")
    assert cleared.status_code == 200
    assert (
        await service.delete("/api/preferences/providers/dashscope")
    ).status_code == 404


async def test_models_and_active_selection(service) -> None:
    added = await service.put(
        "/api/preferences/providers/dashscope/models/my-model",
        json={"source": "extra", "name": "My Model", "context_size": 64000},
    )
    assert added.status_code == 200, added.text
    assert added.json()["model"]["source"] == "extra"
    models = await service.get("/api/preferences/providers/dashscope/models")
    assert models.status_code == 200, models.text
    ids = {m["id"] for m in models.json()["models"]}
    assert {"my-model", "qwen-plus"} <= ids

    selection = {"default_provider_id": "dashscope", "default_model_id": "my-model"}
    blocked = await service.put("/api/preferences/active-models", json=selection)
    assert blocked.status_code == 400
    saved = await service.put(
        "/api/preferences/providers/dashscope", json={"api_key": "sk-k"}
    )
    assert saved.status_code == 200, saved.text
    ok = await service.put("/api/preferences/active-models", json=selection)
    assert ok.status_code == 200, ok.text
    active = await service.get("/api/preferences/active-models")
    assert active.json()["active_models"]["default_model_id"] == "my-model"


async def test_service_builder_resolves_persisted_preferences(
    service, tmp_path, monkeypatch
) -> None:
    for path, body in [
        ("providers/dashscope", {"api_key": "sk-k"}),
        (
            "active-models",
            {"default_provider_id": "dashscope", "default_model_id": "qwen-plus"},
        ),
    ]:
        response = await service.put("/api/preferences/" + path, json=body)
        assert response.status_code == 200, response.text

    factory = get_session_factory()
    async with factory() as db:
        config = (await PreferencesRepository(db, TEST_IDENTITY).load()).resolve()
    paths = UserAgentPaths.from_identity(
        home=tmp_path,
        identity=TEST_IDENTITY,
        agent_id="default",
        session_id="ses-model-test",
    )
    context = RunContext(
        session_id=paths.session_id,
        chat_id="chat-model-test",
        paths=paths,
        workspace=SimpleNamespace(workdir=str(paths.workspace)),
        identity=TEST_IDENTITY,
        session_factory=factory,
        user_runtime_config=config,
    )
    model = FakeTextModel()
    build_model = Mock(return_value=model)
    monkeypatch.setattr(data_agent, "build_model", build_model)
    monkeypatch.setattr(data_agent, "_middlewares", lambda *_: [])
    builder = data_agent.DataAgentBuilder()
    monkeypatch.setattr(builder, "build_toolkit", AsyncMock(return_value=Toolkit()))

    agent = await builder.build(
        run_context=context, envelope=object(), state=AgentState()
    )

    assert agent.model is model
    active = build_model.call_args.args[0]
    assert active.model_id == "qwen-plus" and active.api_key == "sk-k"
    assert build_model.call_args.kwargs["usage"] == context.model_usage(
        ModelUsageSource.DATA_AGENT
    )
    builder.build_toolkit.assert_awaited_once_with(run_context=context)
