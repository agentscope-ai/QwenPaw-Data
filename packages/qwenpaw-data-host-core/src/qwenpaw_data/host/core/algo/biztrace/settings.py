# -*- coding: utf-8 -*-
"""Feature switches, budgets and vocabulary config for BizTrace.

The model is not configured here: the host builds it from the user's
preferences and hands it to the Transformer.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

CONVERTER_QUEUE_SIZE = 2048
SEGMENT_QUEUE_SIZE = 1024
PRESENTATION_TIMEOUT_SECONDS = 5.0
JUDGE_TIMEOUT_SECONDS = 30.0
CLASSIFY_TIMEOUT_SECONDS = 30.0
EXTRACT_TIMEOUT_SECONDS = 60.0
FLUSH_BUDGET_SECONDS = 75.0
# Overall cap on one pipeline flush; backstop behind the middleware join (90s)
# so a hung worker cannot leak the flush task.
PIPELINE_FLUSH_TIMEOUT_SECONDS = 120.0
# Cap on waiting for an in-flight confidence grade so a slow score
# cannot eat a segment whose extraction already finished.
SCORE_JOIN_BUDGET_SECONDS = 15.0


class BizTraceSettings(BaseSettings):
    """Runtime configuration; every knob degrades to a safe default."""

    # Blank env values (``QWENPAW_DATA_SEGMENT_MAX_SPAN=``) mean "unset" and
    # keep the default instead of failing validation, as in 0.3.
    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_", extra="ignore", env_ignore_empty=True
    )

    biz_trace_enabled: bool = True
    trace2segment_enabled: bool = True

    biz_trace_log_dir: str | None = None

    segment_max_span: int = 100
    segment_extract_concurrency: int = 2
    segment_prompt_lang: Literal["zh", "en"] = "zh"

    biz_link_enabled: bool = True
    biz_link_base_url: str | None = None
    biz_link_datasource_id: str | None = None
    biz_link_ttl: float = 300.0


def resolve_link_base_url(settings: BizTraceSettings) -> str:
    """Origin the entity links point at; defaults to the Context frontend."""
    from qwenpaw_data.host.core.settings import get_settings

    configured = (settings.biz_link_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return get_settings().cm.client_cm_frontend_url()


__all__ = [
    "BizTraceSettings",
    "CLASSIFY_TIMEOUT_SECONDS",
    "CONVERTER_QUEUE_SIZE",
    "EXTRACT_TIMEOUT_SECONDS",
    "FLUSH_BUDGET_SECONDS",
    "JUDGE_TIMEOUT_SECONDS",
    "PIPELINE_FLUSH_TIMEOUT_SECONDS",
    "PRESENTATION_TIMEOUT_SECONDS",
    "SCORE_JOIN_BUDGET_SECONDS",
    "SEGMENT_QUEUE_SIZE",
    "resolve_link_base_url",
]
