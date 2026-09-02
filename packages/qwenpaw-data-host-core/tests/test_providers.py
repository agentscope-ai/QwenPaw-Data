from __future__ import annotations

from typing import Any

import pytest

from qwenpaw_data.host.core.providers import (
    BUILTIN_PROVIDERS,
    provider_registry,
    resolve_active_model,
)
from qwenpaw_data.host.core.providers import factory as factory_module
from qwenpaw_data.host.core.providers.registry import ActiveModel


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


def _active(provider_id: str, **overrides: Any) -> ActiveModel:
    values: dict[str, Any] = {
        "provider_id": provider_id,
        "model_id": "some-model",
        "api_key": "some-key",
        "base_url": "https://example.test/v1",
        "chat_model": "FakeChatModel",
        "name": "Some Model",
    }
    values.update(overrides)
    return ActiveModel(**values)


def test_build_model_openai() -> None:
    result = factory_module.build_model(_active("openai"))
    assert isinstance(result, FakeChatModel)
    assert result.kwargs["model"] == "some-model"
    assert result.kwargs["stream"] is True
    assert result.kwargs["credential"].kwargs == {
        "api_key": "some-key",
        "base_url": "https://example.test/v1",
    }
    assert result.kwargs["parameters"].kwargs == {"parallel_tool_calls": False}


def test_build_model_dashscope_without_base_url() -> None:
    result = factory_module.build_model(_active("dashscope", base_url=""))
    assert isinstance(result, FakeChatModel)
    assert result.kwargs["credential"].kwargs == {"api_key": "some-key"}


def test_build_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider_id"):
        factory_module.build_model(_active("unknown"))


def test_build_model_requires_api_key_and_model_id() -> None:
    with pytest.raises(ValueError, match="api_key"):
        factory_module.build_model(_active("openai", api_key=""))
    with pytest.raises(ValueError, match="model_id"):
        factory_module.build_model(_active("openai", model_id=""))


def test_registry_lookup() -> None:
    assert provider_registry.get("dashscope") is BUILTIN_PROVIDERS[0]
    assert provider_registry.get("nope") is None
    with pytest.raises(ValueError, match="unknown provider_id"):
        provider_registry.require("nope")


def test_resolve_active_model_builtin_defaults() -> None:
    active = resolve_active_model("dashscope", "qwen-max", api_key=" key ")
    assert active.api_key == "key"
    assert active.base_url == provider_registry.require("dashscope").base_url
    assert active.chat_model == "DashScopeChatModel"
    assert active.name == "Qwen Max"


def test_resolve_active_model_extra_model_and_base_url_override() -> None:
    active = resolve_active_model(
        "openai",
        "my-custom-model",
        api_key="key",
        base_url="https://proxy.test/v1",
    )
    assert active.name == "my-custom-model"
    assert active.base_url == "https://proxy.test/v1"


def test_resolve_active_model_validation() -> None:
    with pytest.raises(ValueError, match="unknown provider_id"):
        resolve_active_model("nope", "m", api_key="key")
    with pytest.raises(ValueError, match="provider is not configured"):
        resolve_active_model("openai", "m", api_key="  ")
    with pytest.raises(ValueError, match="model_id is required"):
        resolve_active_model("openai", " ", api_key="key")
