# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from agentscope.model import DashScopeChatModel, OpenAIChatModel

from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext, ModelUsageSource
from qwenpaw_data.host.core.domain.preference import ActiveModel

_USAGE = ModelUsageContext(
    source=ModelUsageSource.DATA_AGENT,
    identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
    session_id="ses-1",
    chat_id="chat-1",
)


def _model(**overrides: object) -> ActiveModel:
    values: dict[str, object] = {
        "provider_id": "openai",
        "model_id": "gpt-4o",
        "api_key": "secret",
        "base_url": "https://models.example.test/v1",
        "chat_model": "OpenAIChatModel",
        "name": "GPT-4o",
    }
    values.update(overrides)
    return ActiveModel(**values)  # type: ignore[arg-type]


def test_build_openai_model() -> None:
    model = build_model(_model(), usage=_USAGE)

    assert isinstance(model, OpenAIChatModel)
    assert model.model == "gpt-4o"
    assert model.credential.base_url == "https://models.example.test/v1"
    assert model.credential.api_key.get_secret_value() == "secret"
    assert model.parameters.thinking_enable is False


def test_build_dashscope_model() -> None:
    model = build_model(
        _model(
            provider_id="dashscope",
            model_id="qwen-plus",
            chat_model="DashScopeChatModel",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        usage=_USAGE,
    )

    assert isinstance(model, DashScopeChatModel)
    assert model.model == "qwen-plus"
    assert model.parameters.thinking_enable is False


def test_build_dashscope_model_enables_thinking() -> None:
    model = build_model(
        _model(
            provider_id="dashscope",
            model_id="qwen3.8-max",
            chat_model="DashScopeChatModel",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            thinking_enabled=True,
        ),
        usage=_USAGE,
    )

    assert isinstance(model, DashScopeChatModel)
    assert model.parameters.thinking_enable is True


def test_build_openai_model_disables_thinking() -> None:
    model = build_model(_model(thinking_enabled=False), usage=_USAGE)

    assert isinstance(model, OpenAIChatModel)
    assert model.parameters.thinking_enable is False


def test_build_model_rejects_unknown_provider() -> None:
    # ``context_size`` is pinned so the model reaches provider dispatch; an
    # unknown provider is not in the builtin registry to supply one itself.
    with pytest.raises(ValueError, match="unsupported provider_id"):
        build_model(_model(provider_id="unknown", context_size=128_000), usage=_USAGE)


def test_build_model_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        build_model(_model(api_key=""), usage=_USAGE)
