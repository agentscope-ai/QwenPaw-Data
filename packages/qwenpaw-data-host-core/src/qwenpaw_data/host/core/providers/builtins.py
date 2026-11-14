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
        BuiltinModel(
            id="qwen3.8-max",
            name="Qwen3.8 Max",
            context_size=1_000_000,
            input_types=["text/plain", "image/*", "video/*"],
        ),
        BuiltinModel(id="qwen3.7-max", name="Qwen3.7 Max", context_size=1_000_000),
        BuiltinModel(
            id="qwen3.7-plus",
            name="Qwen3.7 Plus",
            context_size=1_000_000,
            input_types=["text/plain", "image/*", "video/*"],
        ),
        BuiltinModel(
            id="qwen3.7-flash",
            name="Qwen3.7 Flash",
            context_size=1_000_000,
            input_types=["text/plain", "image/*", "video/*"],
        ),
        BuiltinModel(
            id="qwen3.6-plus",
            name="Qwen3.6 Plus",
            context_size=1_000_000,
            input_types=["text/plain", "image/*", "video/*"],
        ),
        BuiltinModel(id="qwen-plus", name="Qwen Plus", context_size=1_000_000),
        BuiltinModel(id="qwen-max", name="Qwen Max", context_size=32_768),
        BuiltinModel(
            id="deepseek-v4-pro", name="DeepSeek-V4 Pro", context_size=1_000_000
        ),
        BuiltinModel(
            id="deepseek-v4-flash", name="DeepSeek-V4 Flash", context_size=1_000_000
        ),
        BuiltinModel(
            id="kimi-k2.6",
            name="Kimi K2.6",
            context_size=262_144,
            input_types=["text/plain", "image/*", "video/*"],
        ),
        BuiltinModel(id="MiniMax-M2.5", name="MiniMax M2.5", context_size=196_608),
        BuiltinModel(id="glm-5.3", name="GLM-5.3", context_size=1_048_576),
        BuiltinModel(id="glm-5.2", name="GLM-5.2", context_size=1_048_576),
        BuiltinModel(
            id="glm-5.2-fast-preview",
            name="GLM-5.2 Fast Preview",
            context_size=1_048_576,
        ),
        BuiltinModel(id="glm-5.1", name="GLM-5.1", context_size=202_745),
        BuiltinModel(id="glm-5", name="GLM-5", context_size=198_000),
    ),
)

PROVIDER_OPENAI = BuiltinProvider(
    id="openai",
    name="OpenAI Compatible",
    base_url="",
    chat_model="OpenAIChatModel",
    api_key_prefix="sk-",
    models=(
        # GPT-5 / GPT-4o families accept image input; GPT-4.1 is text-only.
        BuiltinModel(
            id="gpt-5.4",
            name="GPT-5.4",
            context_size=1_050_000,
            input_types=["text/plain", "image/*"],
        ),
        BuiltinModel(
            id="gpt-5.2",
            name="GPT-5.2",
            context_size=400_000,
            input_types=["text/plain", "image/*"],
        ),
        BuiltinModel(
            id="gpt-5-mini",
            name="GPT-5 Mini",
            context_size=400_000,
            input_types=["text/plain", "image/*"],
        ),
        BuiltinModel(
            id="gpt-4.1",
            name="GPT-4.1",
            context_size=1_047_576,
            input_types=["text/plain", "image/*"],
        ),
        BuiltinModel(
            id="gpt-4o",
            name="GPT-4o",
            context_size=128_000,
            input_types=["text/plain", "image/*"],
        ),
        BuiltinModel(
            id="gpt-4o-mini",
            name="GPT-4o Mini",
            context_size=128_000,
            input_types=["text/plain", "image/*"],
        ),
    ),
)

BUILTIN_PROVIDERS: tuple[BuiltinProvider, ...] = (
    PROVIDER_DASHSCOPE,
    PROVIDER_OPENAI,
)

if not BUILTIN_PROVIDERS:
    raise RuntimeError("provider registry is empty")

provider_registry = ProviderRegistry(providers=BUILTIN_PROVIDERS)
