# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.providers.registry import (
    BuiltinModel,
    BuiltinProvider,
    ProviderRegistry,
)

PROVIDER_DASHSCOPE = BuiltinProvider(
    id="dashscope",
    name="DashScope",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    chat_model="DashScopeChatModel",
    api_key_prefix="sk",
    models=(
        BuiltinModel(id="qwen3.8-plus", name="Qwen3.8 Plus"),
        BuiltinModel(id="qwen3.6-plus", name="Qwen3.6 Plus"),
        BuiltinModel(id="qwen-plus", name="Qwen Plus"),
        BuiltinModel(id="qwen-max", name="Qwen Max"),
        BuiltinModel(id="qwen3.7-flash", name="Qwen3.7 Flash"),
        BuiltinModel(id="glm-5.2", name="GLM-5.2"),
        BuiltinModel(id="glm-5.2-fast-preview", name="GLM-5.2 Fast Preview"),
        BuiltinModel(id="glm-5.1", name="GLM-5.1"),
        BuiltinModel(id="glm-5", name="GLM-5"),
    ),
)

PROVIDER_OPENAI = BuiltinProvider(
    id="openai",
    name="OpenAI Compatible",
    base_url="",
    chat_model="OpenAIChatModel",
    api_key_prefix="sk-",
    models=(
        BuiltinModel(id="gpt-4o", name="GPT-4o"),
        BuiltinModel(id="gpt-4o-mini", name="GPT-4o Mini"),
    ),
)

BUILTIN_PROVIDERS: tuple[BuiltinProvider, ...] = (
    PROVIDER_DASHSCOPE,
    PROVIDER_OPENAI,
)

if not BUILTIN_PROVIDERS:
    raise RuntimeError("provider registry is empty")

provider_registry = ProviderRegistry(providers=BUILTIN_PROVIDERS)
