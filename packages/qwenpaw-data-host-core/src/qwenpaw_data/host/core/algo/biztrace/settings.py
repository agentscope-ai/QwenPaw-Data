# -*- coding: utf-8 -*-
"""Feature switches, budgets and vocabulary config for BizTrace.

The model is not configured here: the host builds it from the user's
preferences and hands it to the Transformer.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

CONVERTER_QUEUE_SIZE = 2048
SEGMENT_QUEUE_SIZE = 1024
PRESENTATION_TIMEOUT_SECONDS = 5.0
JUDGE_TIMEOUT_SECONDS = 30.0
EXTRACT_TIMEOUT_SECONDS = 60.0
FLUSH_BUDGET_SECONDS = 75.0

_ENV_PREFIX = "QWENPAW_DATA_"
CM_FRONTEND_URL_ENV = f"{_ENV_PREFIX}CM_FRONTEND_URL"
DEFAULT_CM_FRONTEND_URL = "http://localhost:3000"


def _env(name: str) -> str | None:
    raw = os.environ.get(f"{_ENV_PREFIX}{name.upper()}")
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_lang(name: str, default: Literal["zh", "en"]) -> Literal["zh", "en"]:
    raw = (_env(name) or "").lower()
    return raw if raw in ("zh", "en") else default  # type: ignore[return-value]


class BizTraceSettings(BaseModel):
    """Runtime configuration; every knob degrades to a safe default.

    Defaults come from ``QWENPAW_DATA_*`` env vars; constructor kwargs win.
    """

    biz_trace_enabled: bool = Field(
        default_factory=lambda: _env_bool("biz_trace_enabled", True)
    )
    trace2segment_enabled: bool = Field(
        default_factory=lambda: _env_bool("trace2segment_enabled", True)
    )

    biz_trace_log_dir: str | None = Field(
        default_factory=lambda: _env("biz_trace_log_dir")
    )

    segment_max_span: int = Field(
        default_factory=lambda: _env_int("segment_max_span", 100)
    )
    segment_extract_concurrency: int = Field(
        default_factory=lambda: _env_int("segment_extract_concurrency", 2)
    )
    segment_prompt_lang: Literal["zh", "en"] = Field(
        default_factory=lambda: _env_lang("segment_prompt_lang", "zh")
    )

    biz_link_enabled: bool = Field(
        default_factory=lambda: _env_bool("biz_link_enabled", True)
    )
    biz_link_base_url: str | None = Field(
        default_factory=lambda: _env("biz_link_base_url")
    )
    biz_link_datasource_id: str | None = Field(
        default_factory=lambda: _env("biz_link_datasource_id")
    )
    biz_link_ttl: float = Field(
        default_factory=lambda: _env_float("biz_link_ttl", 300.0)
    )


def resolve_link_base_url(settings: BizTraceSettings) -> str:
    """Origin the entity links point at; defaults to the Context frontend."""
    configured = (settings.biz_link_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    frontend = (os.environ.get(CM_FRONTEND_URL_ENV) or "").strip()
    return (frontend or DEFAULT_CM_FRONTEND_URL).rstrip("/")


__all__ = [
    "BizTraceSettings",
    "CM_FRONTEND_URL_ENV",
    "CONVERTER_QUEUE_SIZE",
    "DEFAULT_CM_FRONTEND_URL",
    "EXTRACT_TIMEOUT_SECONDS",
    "FLUSH_BUDGET_SECONDS",
    "JUDGE_TIMEOUT_SECONDS",
    "PRESENTATION_TIMEOUT_SECONDS",
    "SEGMENT_QUEUE_SIZE",
    "resolve_link_base_url",
]
