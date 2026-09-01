# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from qwenpaw_data.host.core.api.models.common import ApiModel


class ProviderModelSchema(ApiModel):
    id: str
    name: str
    source: Literal["catalog", "extra", "override"]
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
