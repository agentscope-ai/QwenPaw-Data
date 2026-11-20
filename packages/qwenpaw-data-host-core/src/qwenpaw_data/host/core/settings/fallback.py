# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 0.3 env model contract (OSS #40): provider defaults to "openai"; the generic
# LLM_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL names are honored after the
# QWENPAW_DATA_* names. ``validation_alias`` bypasses env_prefix, so the
# primary names are spelled out in full.
DEFAULT_FALLBACK_PROVIDER = "openai"
# Context window assumed for env models that are not in the provider catalog
# (agentscope's ChatModelBase default).
DEFAULT_FALLBACK_CONTEXT_SIZE = 32768


class FallbackSettings(BaseSettings):
    """Host-level LLM fallback when the user has no usable preferences."""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_",
        extra="ignore",
        # Fields like model_name collide with Pydantic's model_ namespace.
        protected_namespaces=(),
        populate_by_name=True,
        # A blank QWENPAW_DATA_MODEL_NAME falls through to LLM_MODEL (0.3).
        env_ignore_empty=True,
    )

    fallback_enabled: bool = True
    model_provider: str = DEFAULT_FALLBACK_PROVIDER
    model_name: str = Field(
        default="",
        validation_alias=AliasChoices("QWENPAW_DATA_MODEL_NAME", "LLM_MODEL"),
    )
    model_light_name: str = ""
    model_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("QWENPAW_DATA_MODEL_API_KEY", "OPENAI_API_KEY"),
        repr=False,
    )
    model_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "QWENPAW_DATA_MODEL_BASE_URL", "OPENAI_BASE_URL"
        ),
    )
    model_context_size: int = Field(default=DEFAULT_FALLBACK_CONTEXT_SIZE, gt=0)

    def provider_id(self) -> str:
        return (self.model_provider or "").strip().lower() or DEFAULT_FALLBACK_PROVIDER
