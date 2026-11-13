# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from qwenpaw_data.host.core.api.models.common import ApiModel
from qwenpaw_data.host.core.domain.tool_confirmation import ToolConfirmationLevel


class ProviderModelSchema(ApiModel):
    id: str
    name: str
    source: Literal["catalog", "extra", "override"]
    context_size: int | None = None
    thinking_enabled: bool | None = None
    generate_kwargs: dict[str, Any] = Field(default_factory=dict)
    chat_model: str | None = None


class ProviderSchema(ApiModel):
    id: str
    name: str
    chat_model: str
    api_key_prefix: str
    catalog_base_url: str
    base_url: str
    configured: bool
    api_key_masked: str
    models: list[ProviderModelSchema]


class UpsertProviderRequest(ApiModel):
    api_key: str | None = None
    base_url: str | None = None


class UpsertProviderModelRequest(ApiModel):
    source: Literal["extra", "override"]
    name: str | None = None
    context_size: int | None = Field(default=None, gt=0)
    thinking_enabled: bool | None = None
    generate_kwargs: dict[str, Any] | None = None


class ActiveModelsSchema(ApiModel):
    default_provider_id: str | None = None
    default_model_id: str | None = None
    light_provider_id: str | None = None
    light_model_id: str | None = None


class SetActiveModelsRequest(ApiModel):
    default_provider_id: str
    default_model_id: str
    light_provider_id: str | None = None
    light_model_id: str | None = None


class RuntimeSettingsSchema(ApiModel):
    react_max_iters: int
    llm_retry_enabled: bool
    llm_max_retries: int
    context_trigger_ratio: float
    context_reserve_ratio: float
    tool_result_limit: int
    tool_timeout_enabled: bool
    tool_timeout_seconds: float
    tool_confirmation_level: ToolConfirmationLevel


class SetRuntimeSettingsRequest(ApiModel):
    react_max_iters: int | None = None
    llm_retry_enabled: bool | None = None
    llm_max_retries: int | None = None
    context_trigger_ratio: float | None = Field(default=None, gt=0, le=0.9)
    context_reserve_ratio: float | None = Field(default=None, gt=0, lt=0.9)
    tool_result_limit: int | None = Field(default=None, gt=0)
    tool_timeout_enabled: bool | None = None
    tool_timeout_seconds: float | None = Field(default=None, ge=1, allow_inf_nan=False)
    tool_confirmation_level: ToolConfirmationLevel | None = None
