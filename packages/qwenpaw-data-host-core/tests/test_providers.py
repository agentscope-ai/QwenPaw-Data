from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from qwenpaw_data.host.core.providers import (
    BUILTIN_PROVIDERS,
    provider_registry,
)
from qwenpaw_data.host.core.providers import factory as factory_module
from qwenpaw_data.host.core.domain.preference import (
    ActiveModel,
    ModelOverride,
    ProviderCredential,
    UserPreferences,
)
from qwenpaw_data.host.core.domain.model_usage import (
    ModelUsageContext,
    ModelUsageSource,
)
from qwenpaw_data.host.core.settings import clear_settings

from .conftest import TEST_IDENTITY

USAGE = ModelUsageContext(
    source=ModelUsageSource.DATA_AGENT,
    identity=TEST_IDENTITY,
    session_id="session-test",
    chat_id="chat-test",
)


class FakeCredential:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeParameters:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeChatModel:
    Parameters = FakeParameters

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def fake_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_module, "OpenAICredential", FakeCredential)
    monkeypatch.setattr(factory_module, "OpenAIChatModel", FakeChatModel)
    monkeypatch.setattr(factory_module, "DashScopeCredential", FakeCredential)
    monkeypatch.setattr(factory_module, "DashScopeChatModel", FakeChatModel)
    monkeypatch.setattr(
        factory_module,
        "attach_model_usage",
        Mock(side_effect=lambda model, *_a, **_k: model),
    )
    monkeypatch.setattr(
        factory_module,
        "attach_generation_span",
        Mock(side_effect=lambda model, *_a: model),
    )
    monkeypatch.setenv("QWENPAW_DATA_FALLBACK_ENABLED", "false")
    clear_settings()


def _active(provider_id: str, **overrides: Any) -> ActiveModel:
    values: dict[str, Any] = {
        "provider_id": provider_id,
        "model_id": "some-model",
        "api_key": "some-key",
        "base_url": "https://example.test/v1",
        "chat_model": "FakeChatModel",
        "name": "Some Model",
        "context_size": 32768,
    }
    values.update(overrides)
    return ActiveModel(**values)


def test_build_model_openai() -> None:
    result = factory_module.build_model(_active("openai"), usage=USAGE)
    assert isinstance(result, FakeChatModel)
    assert result.kwargs["model"] == "some-model"
    assert result.kwargs["stream"] is True
    assert result.kwargs["credential"].kwargs == {
        "api_key": "some-key",
        "base_url": "https://example.test/v1",
    }
    assert result.kwargs["parameters"].kwargs == {"parallel_tool_calls": False}
    assert result.kwargs["context_size"] == 32768
    factory_module.attach_model_usage.assert_called_once_with(
        result, USAGE, provider_id="openai"
    )
    factory_module.attach_generation_span.assert_called_once_with(result, USAGE)


def test_build_model_dashscope_without_base_url() -> None:
    result = factory_module.build_model(_active("dashscope", base_url=""), usage=USAGE)
    assert isinstance(result, FakeChatModel)
    assert result.kwargs["credential"].kwargs == {"api_key": "some-key"}


def test_build_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider_id"):
        factory_module.build_model(_active("unknown"), usage=USAGE)


def test_build_model_requires_api_key_and_model_id() -> None:
    with pytest.raises(ValueError, match="api_key"):
        factory_module.build_model(_active("openai", api_key=""), usage=USAGE)
    with pytest.raises(ValueError, match="model_id"):
        factory_module.build_model(_active("openai", model_id=""), usage=USAGE)


def test_registry_lookup() -> None:
    assert provider_registry.get("dashscope") is BUILTIN_PROVIDERS[0]
    assert provider_registry.get("nope") is None
    with pytest.raises(ValueError, match="unknown provider_id"):
        provider_registry.require("nope")


def test_dashscope_catalog_includes_current_internal_models() -> None:
    models = provider_registry.require("dashscope")
    expected = {
        "qwen4-max": (1_000_000, ["text/plain", "image/*", "video/*"]),
        "qwen3.8-flash": (1_000_000, ["text/plain", "image/*", "video/*"]),
        "deepseek-v4.1-flash": (1_000_000, ["text/plain", "image/*"]),
        "kimi-k3.1-pro": (1_048_576, ["text/plain", "image/*"]),
        "kimi-k3": (1_048_576, ["text/plain", "image/*"]),
        "MiniMax/MiniMax-M3.1-Flash": (1_000_000, ["text/plain", "image/*", "video/*"]),
    }
    for model_id, (context_size, input_types) in expected.items():
        model = models.find_model(model_id)
        assert model is not None
        assert (model.context_size, model.input_types) == (context_size, input_types)
    assert models.find_model("qwen3.7-max") is not None


def test_preferences_resolve_builtin_defaults() -> None:
    active = (
        UserPreferences(
            user_id=TEST_IDENTITY.user_id,
            providers={"dashscope": ProviderCredential(api_key=" key ", base_url=None)},
            default_provider_id="dashscope",
            default_model_id="qwen-plus",
        )
        .resolve()
        .default
    )
    assert active.api_key == "key"
    assert active.base_url == provider_registry.require("dashscope").base_url
    assert active.chat_model == "DashScopeChatModel"
    assert (
        active.name
        == provider_registry.require("dashscope").find_model("qwen-plus").name
    )
    assert active.context_size > 0


def test_preferences_resolve_registered_extra_model_and_base_url_override() -> None:
    active = (
        UserPreferences(
            user_id=TEST_IDENTITY.user_id,
            providers={
                "openai": ProviderCredential(
                    api_key="key",
                    base_url="https://proxy.test/v1",
                )
            },
            models={
                ("openai", "my-custom-model"): ModelOverride(
                    source="extra",
                    name=None,
                    context_size=64000,
                )
            },
            default_provider_id="openai",
            default_model_id="my-custom-model",
        )
        .resolve()
        .default
    )
    assert active.name == "my-custom-model"
    assert active.base_url == "https://proxy.test/v1"
    assert active.context_size == 64000


def test_preferences_resolve_validation() -> None:
    with pytest.raises(ValueError, match="unknown provider_id"):
        UserPreferences(
            user_id="u",
            default_provider_id="nope",
            default_model_id="m",
        ).resolve()
    with pytest.raises(ValueError, match="provider is not configured"):
        UserPreferences(
            user_id="u",
            default_provider_id="openai",
            default_model_id="m",
            providers={"openai": ProviderCredential(api_key="  ", base_url=None)},
        ).resolve()
    with pytest.raises(ValueError, match="active models are not configured"):
        UserPreferences(user_id="u", default_provider_id="openai").resolve()


@pytest.mark.parametrize("registered", [False, True])
def test_custom_model_requires_registration_and_context_size(registered) -> None:
    prefs = UserPreferences(
        user_id="u",
        default_provider_id="openai",
        default_model_id="custom",
        providers={"openai": ProviderCredential(api_key="key", base_url=None)},
    )
    if registered:
        prefs.models[("openai", "custom")] = ModelOverride(
            source="extra", name="Custom"
        )
    expected = "context_size is required" if registered else "model is not available"
    with pytest.raises(ValueError, match=expected):
        prefs.resolve()
