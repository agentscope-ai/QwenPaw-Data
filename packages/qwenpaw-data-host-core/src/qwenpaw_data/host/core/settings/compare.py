# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class CompareSettings(BaseSettings):
    """Artifact-compare Host settings (including the LLM recommendations).

    The defaults are restated here rather than imported from the compare
    router/algo: reading them from those modules would make ``get_settings()``
    pull in the LLM stack and the agent runtime, which only an actual
    recommendation call needs. They must stay in step with those modules' own
    constants.
    """

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_COMPARE_", extra="ignore")

    # The advisory compare-recommendation model call: how long to wait and how
    # many attempts before giving up (recommendations degrade to empty).
    recommend_timeout_sec: float = 20.0
    recommend_attempts: int = 2
    # In-process cache for recommendations. Successes live for the full TTL;
    # empty results (no usable model / failed call) are held only for the short
    # negative window so the picker retries soon instead of hammering the model.
    recommend_cache_ttl_sec: float = 300.0
    recommend_cache_negative_ttl_sec: float = 30.0
    recommend_cache_max: int = 128
    # Local documents uploaded from the compare picker are capped here.
    upload_max_bytes: int = 20 * 1024 * 1024  # 20 MiB


__all__ = ["CompareSettings"]
