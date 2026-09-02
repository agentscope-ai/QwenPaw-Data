# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from qwenpaw_data.host.core.providers.registry import ActiveModel
from qwenpaw_data.host.core.providers.builtins import provider_registry


@dataclass(frozen=True)
class ProviderCredential:
    api_key: str
    base_url: Optional[str]


@dataclass(frozen=True)
class ModelOverride:
    source: str
    name: Optional[str]
    thinking_enabled: Optional[bool] = None
    generate_kwargs: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class UserRuntimeConfig:
    """Resolved model selection for background work (e.g. follow-up)."""

    default: ActiveModel | None = None
    light: ActiveModel | None = None


@dataclass
class UserPreferences:
    """Loaded user preference state for provider credentials and models."""

    user_id: str
    providers: dict[str, ProviderCredential] = field(default_factory=dict)
    models: dict[tuple[str, str], ModelOverride] = field(default_factory=dict)
    default_provider_id: Optional[str] = None
    default_model_id: Optional[str] = None
    light_provider_id: Optional[str] = None
    light_model_id: Optional[str] = None

    def active_default(self) -> ActiveModel | None:
        """The configured default model, or None when not set up."""
        if not self.default_provider_id or not self.default_model_id:
            return None
        return self._active_model(self.default_provider_id, self.default_model_id)

    def runtime_config(self) -> UserRuntimeConfig:
        """Resolve default/light selections; unresolvable sides become None."""
        default = light = None
        try:
            default = self.active_default()
        except ValueError:
            pass
        if self.light_provider_id and self.light_model_id:
            try:
                light = self._active_model(
                    self.light_provider_id, self.light_model_id
                )
            except ValueError:
                pass
        return UserRuntimeConfig(default=default, light=light)

    def validate_selection(self) -> None:
        """Raise ValueError when the active selection cannot resolve."""
        if not self.default_provider_id or not self.default_model_id:
            raise ValueError("active models are not configured")
        if bool(self.light_provider_id) != bool(self.light_model_id):
            raise ValueError(
                "light_provider_id and light_model_id must be set together"
            )
        self._active_model(self.default_provider_id, self.default_model_id)
        if self.light_provider_id and self.light_model_id:
            self._active_model(self.light_provider_id, self.light_model_id)

    def _active_model(self, provider_id: str, model_id: str) -> ActiveModel:
        spec = provider_registry.require(provider_id)
        credential = self.providers.get(provider_id)
        if credential is None or not credential.api_key.strip():
            raise ValueError(f"provider is not configured: {provider_id}")
        base_url = (
            spec.base_url if credential.base_url is None else credential.base_url
        )
        override = self.models.get((provider_id, model_id))
        builtin = spec.find_model(model_id)
        if builtin is None:
            if override is None or override.source != "extra":
                raise ValueError(f"model is not available: {provider_id}/{model_id}")
            name = override.name or model_id
        else:
            name = (
                override.name
                if override is not None and override.name
                else builtin.name
            )
        return ActiveModel(
            provider_id=provider_id,
            model_id=model_id,
            api_key=credential.api_key.strip(),
            base_url=base_url,
            chat_model=spec.chat_model,
            name=name,
        )
