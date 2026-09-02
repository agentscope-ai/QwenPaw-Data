from __future__ import annotations

import os
from collections.abc import Mapping

from agentscope.model import ChatModelBase

from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.providers.registry import ActiveModel

_PROVIDER_ENV = "QWENPAW_DATA_MODEL_PROVIDER"
_MODEL_ENV = "QWENPAW_DATA_MODEL_NAME"
_API_KEY_ENV = "QWENPAW_DATA_MODEL_API_KEY"
_BASE_URL_ENV = "QWENPAW_DATA_MODEL_BASE_URL"
_FALLBACK_MODEL_ENV = "LLM_MODEL"
_FALLBACK_API_KEY_ENV = "OPENAI_API_KEY"
_FALLBACK_BASE_URL_ENV = "OPENAI_BASE_URL"
_DEFAULT_PROVIDER = "openai"
_SUPPORTED_PROVIDERS = ("dashscope", "openai")
_CHAT_MODEL_BY_PROVIDER = {
    "dashscope": "DashScopeChatModel",
    "openai": "OpenAIChatModel",
}


def build_model_from_env(
    env: Mapping[str, str] | None = None,
) -> ChatModelBase:
    values = os.environ if env is None else env
    provider = (_env(values, _PROVIDER_ENV) or _DEFAULT_PROVIDER).lower()
    model_name = _env(values, _MODEL_ENV) or _env(values, _FALLBACK_MODEL_ENV)
    api_key = _env(values, _API_KEY_ENV) or _env(values, _FALLBACK_API_KEY_ENV)
    base_url = _env(values, _BASE_URL_ENV) or _env(
        values,
        _FALLBACK_BASE_URL_ENV,
    )

    missing = [
        name
        for name, value in (
            (f"{_MODEL_ENV} (or {_FALLBACK_MODEL_ENV})", model_name),
            (f"{_API_KEY_ENV} (or {_FALLBACK_API_KEY_ENV})", api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "QwenPaw Data model is not configured; pass model=... to "
            "QwenPawDataHost(...) "
            "or set "
            + ", ".join(missing)
            + ".",
        )

    if provider not in _SUPPORTED_PROVIDERS:
        raise RuntimeError(
            f"Unsupported {_PROVIDER_ENV}={provider!r}; supported providers: dashscope, openai.",
        )

    return build_model(
        ActiveModel(
            provider_id=provider,
            model_id=model_name,
            api_key=api_key,
            base_url=base_url,
            chat_model=_CHAT_MODEL_BY_PROVIDER[provider],
            name=model_name,
        ),
    )


def _env(values: Mapping[str, str], name: str) -> str:
    return str(values.get(name, "")).strip()
