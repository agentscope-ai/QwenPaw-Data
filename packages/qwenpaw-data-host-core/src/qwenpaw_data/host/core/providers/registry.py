# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinModel:
    id: str
    name: str


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


@dataclass(frozen=True)
class ActiveModel:
    provider_id: str
    model_id: str
    api_key: str
    base_url: str
    chat_model: str
    name: str


def resolve_active_model(
    provider_id: str,
    model_id: str,
    *,
    api_key: str,
    base_url: str | None = None,
    name: str | None = None,
) -> ActiveModel:
    from qwenpaw_data.host.core.providers.builtins import provider_registry

    spec = provider_registry.require(provider_id)
    if not model_id or not model_id.strip():
        raise ValueError("model_id is required")
    if not api_key or not api_key.strip():
        raise ValueError(f"provider is not configured: {provider_id}")
    builtin = spec.find_model(model_id)
    resolved_name = name or (builtin.name if builtin is not None else model_id)
    return ActiveModel(
        provider_id=provider_id,
        model_id=model_id,
        api_key=api_key.strip(),
        base_url=spec.base_url if base_url is None else base_url,
        chat_model=spec.chat_model,
        name=resolved_name,
    )
