# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field


# TODO: replace with AgentScope ModelCard
@dataclass(frozen=True)
class BuiltinModel:
    id: str
    name: str
    context_size: int
    input_types: list = field(default_factory=lambda: ["text/plain"])


@dataclass(frozen=True)
class BuiltinProvider:
    id: str
    name: str
    base_url: str
    chat_model: str
    api_key_prefix: str
    models: tuple[BuiltinModel, ...]

    def find_model(self, model_id: str) -> BuiltinModel | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True)
class ProviderRegistry:
    providers: tuple[BuiltinProvider, ...]

    def get(self, provider_id: str) -> BuiltinProvider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def require(self, provider_id: str) -> BuiltinProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider_id: {provider_id}")
        return provider
