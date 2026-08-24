from __future__ import annotations

from typing import Any

import pytest

from qwenpaw_data.host.core import model as model_module


class FakeCredential:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeParameters:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeOpenAIModel:
    Parameters = FakeParameters

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeDashScopeModel:
    Parameters = FakeParameters

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def fake_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_module, "OpenAICredential", FakeCredential)
    monkeypatch.setattr(model_module, "OpenAIChatModel", FakeOpenAIModel)
    monkeypatch.setattr(model_module, "DashScopeCredential", FakeCredential)
    monkeypatch.setattr(model_module, "DashScopeChatModel", FakeDashScopeModel)


def test_openai_compatible_env_is_used_as_default() -> None:
    result = model_module.build_model_from_env(
        {
            "LLM_MODEL": "shared-model",
            "OPENAI_API_KEY": "shared-key",
            "OPENAI_BASE_URL": "https://shared.example/v1",
        },
    )

    assert isinstance(result, FakeOpenAIModel)
    assert result.kwargs["model"] == "shared-model"
    assert result.kwargs["credential"].kwargs == {
        "api_key": "shared-key",
        "base_url": "https://shared.example/v1",
    }


def test_qwenpaw_data_model_env_overrides_shared_values() -> None:
    result = model_module.build_model_from_env(
        {
            "QWENPAW_DATA_MODEL_PROVIDER": "openai",
            "QWENPAW_DATA_MODEL_NAME": "cli-model",
            "QWENPAW_DATA_MODEL_API_KEY": "cli-key",
            "QWENPAW_DATA_MODEL_BASE_URL": "https://cli.example/v1",
            "LLM_MODEL": "shared-model",
            "OPENAI_API_KEY": "shared-key",
            "OPENAI_BASE_URL": "https://shared.example/v1",
        },
    )

    assert isinstance(result, FakeOpenAIModel)
    assert result.kwargs["model"] == "cli-model"
    assert result.kwargs["credential"].kwargs == {
        "api_key": "cli-key",
        "base_url": "https://cli.example/v1",
    }


def test_dashscope_provider_remains_supported() -> None:
    result = model_module.build_model_from_env(
        {
            "QWENPAW_DATA_MODEL_PROVIDER": "dashscope",
            "QWENPAW_DATA_MODEL_NAME": "qwen-model",
            "QWENPAW_DATA_MODEL_API_KEY": "dashscope-key",
        },
    )

    assert isinstance(result, FakeDashScopeModel)
    assert result.kwargs["model"] == "qwen-model"
    assert result.kwargs["credential"].kwargs == {"api_key": "dashscope-key"}


def test_missing_model_config_reports_alternatives_without_secret() -> None:
    secret = "must-not-leak"

    with pytest.raises(RuntimeError) as exc_info:
        model_module.build_model_from_env({"OPENAI_API_KEY": secret})

    message = str(exc_info.value)
    assert "QWENPAW_DATA_MODEL_NAME (or LLM_MODEL)" in message
    assert "QWENPAW_DATA_MODEL_API_KEY" not in message
    assert secret not in message


def test_missing_model_and_api_key_report_both_fallbacks() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        model_module.build_model_from_env({})

    message = str(exc_info.value)
    assert "QWENPAW_DATA_MODEL_NAME (or LLM_MODEL)" in message
    assert "QWENPAW_DATA_MODEL_API_KEY (or OPENAI_API_KEY)" in message


def test_unsupported_provider_does_not_expose_api_key() -> None:
    secret = "must-not-leak"

    with pytest.raises(RuntimeError) as exc_info:
        model_module.build_model_from_env(
            {
                "QWENPAW_DATA_MODEL_PROVIDER": "unsupported",
                "QWENPAW_DATA_MODEL_NAME": "model",
                "QWENPAW_DATA_MODEL_API_KEY": secret,
            },
        )

    message = str(exc_info.value)
    assert "Unsupported QWENPAW_DATA_MODEL_PROVIDER='unsupported'" in message
    assert secret not in message
