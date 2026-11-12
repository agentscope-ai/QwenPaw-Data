# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from qwenpaw_data.host.core.providers import provider_registry
from qwenpaw_data.host.core.settings import get_settings


@dataclass(frozen=True)
class ActiveModel:
    provider_id: str
    model_id: str
    api_key: str = field(repr=False)
    base_url: str
    chat_model: str
    name: str
    thinking_enabled: Optional[bool] = None
    context_size: Optional[int] = None


@dataclass(frozen=True)
class UserRuntimeConfig:
    default: ActiveModel
    light: Optional[ActiveModel]
    react_max_iters: int
    llm_retry_enabled: bool
    llm_max_retries: int
    context_trigger_ratio: float
    context_reserve_ratio: float
    tool_result_limit: int
    tool_timeout_enabled: bool
    tool_timeout_seconds: float
    tool_confirmation_level: str = field(
        default_factory=lambda: get_settings().tool_confirmation.default_level
    )


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
    context_size: Optional[int] = None


@dataclass
class UserPreferences:
    """Loaded user preference state; resolve() builds a run config."""

    user_id: str
    providers: dict[str, ProviderCredential] = field(default_factory=dict)
    models: dict[tuple[str, str], ModelOverride] = field(default_factory=dict)
    default_provider_id: Optional[str] = None
    default_model_id: Optional[str] = None
    light_provider_id: Optional[str] = None
    light_model_id: Optional[str] = None
    react_max_iters: Optional[int] = None
    llm_retry_enabled: Optional[bool] = None
    llm_max_retries: Optional[int] = None
    context_trigger_ratio: Optional[float] = None
    context_reserve_ratio: Optional[float] = None
    tool_result_limit: Optional[int] = None
    tool_timeout_enabled: Optional[bool] = None
    tool_timeout_seconds: Optional[float] = None
    tool_confirmation_level: Optional[str] = None

    def resolve(self) -> UserRuntimeConfig:
        try:
            return self._build()
        except ValueError:
            fb = get_settings().fallback
            # 0.3 env contract: provider defaults to "openai" (FallbackSettings
            # also maps LLM_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL).
            provider_id = fb.provider_id()
            model_id = fb.model_name.strip()
            api_key = fb.model_api_key.strip()
            if (
                not fb.fallback_enabled
                or not provider_id
                or not model_id
                or not api_key
            ):
                raise
            light_model_id = fb.model_light_name.strip() or None
            return UserPreferences(
                user_id=self.user_id,
                providers={
                    provider_id: ProviderCredential(
                        api_key=api_key, base_url=fb.model_base_url.strip() or None
                    )
                },
                models=_with_env_models(
                    self.models,
                    provider_id,
                    [model_id, light_model_id],
                    context_size=fb.model_context_size,
                ),
                default_provider_id=provider_id,
                default_model_id=model_id,
                light_provider_id=provider_id if light_model_id else None,
                light_model_id=light_model_id,
                react_max_iters=self.react_max_iters,
                llm_retry_enabled=self.llm_retry_enabled,
                llm_max_retries=self.llm_max_retries,
                context_trigger_ratio=self.context_trigger_ratio,
                context_reserve_ratio=self.context_reserve_ratio,
                tool_result_limit=self.tool_result_limit,
                tool_timeout_enabled=self.tool_timeout_enabled,
                tool_timeout_seconds=self.tool_timeout_seconds,
                tool_confirmation_level=self.tool_confirmation_level,
            )._build()

    def _build(self) -> UserRuntimeConfig:
        if not self.default_provider_id or not self.default_model_id:
            raise ValueError("active models are not configured")
        if bool(self.light_provider_id) != bool(self.light_model_id):
            raise ValueError(
                "light_provider_id and light_model_id must be set together"
            )
        light = None
        if self.light_provider_id and self.light_model_id:
            light = self._active_model(self.light_provider_id, self.light_model_id)
        runtime = get_settings().runtime
        trigger = _or(self.context_trigger_ratio, runtime.context_trigger_ratio)
        reserve = _or(self.context_reserve_ratio, runtime.context_reserve_ratio)
        if reserve >= trigger:
            raise ValueError(
                "context_reserve_ratio must be smaller than context_trigger_ratio"
            )
        return UserRuntimeConfig(
            default=self._active_model(self.default_provider_id, self.default_model_id),
            light=light,
            react_max_iters=_or(self.react_max_iters, runtime.react_max_iters),
            llm_retry_enabled=_or(self.llm_retry_enabled, runtime.llm_retry_enabled),
            llm_max_retries=_or(self.llm_max_retries, runtime.llm_max_retries),
            context_trigger_ratio=trigger,
            context_reserve_ratio=reserve,
            tool_result_limit=_or(self.tool_result_limit, runtime.tool_result_limit),
            tool_timeout_enabled=_or(
                self.tool_timeout_enabled, runtime.tool_timeout_enabled
            ),
            tool_timeout_seconds=_or(
                self.tool_timeout_seconds, runtime.tool_timeout_seconds
            ),
            tool_confirmation_level=_or(
                self.tool_confirmation_level,
                get_settings().tool_confirmation.default_level,
            ),
        )

    def _active_model(self, provider_id: str, model_id: str) -> ActiveModel:
        spec = provider_registry.require(provider_id)
        credential = self.providers.get(provider_id)
        if credential is None or not credential.api_key.strip():
            raise ValueError(f"provider is not configured: {provider_id}")
        base_url = spec.base_url if credential.base_url is None else credential.base_url
        override = self.models.get((provider_id, model_id))
        builtin = spec.find_model(model_id)
        if builtin is None:
            if override is None or override.source != "extra":
                raise ValueError(f"model is not available: {provider_id}/{model_id}")
            if override.context_size is None:
                raise ValueError(
                    f"context_size is required for extra model: {provider_id}/{model_id}"
                )
            name = override.name or model_id
            context_size = override.context_size
        else:
            name = (
                override.name
                if override is not None and override.name
                else builtin.name
            )
            context_size = builtin.context_size
        return ActiveModel(
            provider_id=provider_id,
            model_id=model_id,
            api_key=credential.api_key.strip(),
            base_url=base_url,
            chat_model=spec.chat_model,
            name=name,
            thinking_enabled=None if override is None else override.thinking_enabled,
            context_size=context_size,
        )


def _or(value: Any, default: Any) -> Any:
    return default if value is None else value


def _with_env_models(
    models: dict[tuple[str, str], ModelOverride],
    provider_id: str,
    model_ids: list[Optional[str]],
    *,
    context_size: int,
) -> dict[tuple[str, str], ModelOverride]:
    """Admit env-configured models that are not in the provider catalog.

    0.3 built any ``LLM_MODEL`` directly; the catalog would otherwise reject
    it. The overrides live only in the resolved run config (never persisted)
    and never replace an override the user saved.
    """
    try:
        spec = provider_registry.require(provider_id)
    except (KeyError, ValueError):
        return models
    merged = dict(models)
    for model_id in model_ids:
        if not model_id or spec.find_model(model_id) is not None:
            continue
        merged.setdefault(
            (provider_id, model_id),
            ModelOverride(source="extra", name=None, context_size=context_size),
        )
    return merged
