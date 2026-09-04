# -*- coding: utf-8 -*-
"""Backend-independent preference mutation rules shared by the stores."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.providers.builtins import provider_registry
from qwenpaw_data.host.core.utils.secrets import encrypt_api_key


def merge_provider_patch(
    *,
    exists: bool,
    current_api_key_enc: str | None,
    current_base_url: str | None,
    patch: dict[str, Any],
    provider_id: str,
) -> tuple[str, str | None]:
    """Return the (api_key_enc, base_url) to store for a provider upsert."""
    provider_registry.require(provider_id)
    api_key = patch.get("api_key") if "api_key" in patch else None
    if not exists and not (api_key and str(api_key).strip()):
        raise ValueError("api_key is required")
    if "base_url" in patch:
        base_url = patch["base_url"]
        base_url = None if base_url is None else (str(base_url).strip() or None)
    else:
        base_url = current_base_url
    api_key_enc = (
        str(current_api_key_enc)
        if api_key is None
        else encrypt_api_key(str(api_key).strip())
    )
    return api_key_enc, base_url


def clean_model_upsert(
    provider_id: str,
    model_id: str,
    *,
    source: str,
    name: str | None,
) -> tuple[str, str | None]:
    """Validate a model upsert; return the cleaned (model_id, name)."""
    provider_registry.require(provider_id)
    model_id = model_id.strip()
    if not model_id:
        raise ValueError("model_id is required")
    if source == "extra" and not (name and name.strip()):
        raise ValueError("name is required for extra models")
    return model_id, None if name is None else name.strip()


def validate_active_selection(prefs: Any) -> None:
    """Raise when the requested active models cannot resolve."""
    prefs.validate_selection()


RUNTIME_SETTING_KEYS = (
    "react_max_iters",
    "llm_retry_enabled",
    "llm_max_retries",
)


def runtime_defaults() -> dict[str, Any]:
    """Instance-wide runtime defaults, overridable via environment."""
    import os

    def _int(name: str, fallback: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        try:
            return int(raw) if raw else fallback
        except ValueError:
            return fallback

    raw_retry = (
        os.environ.get("QWENPAW_DATA_LLM_RETRY_ENABLED") or ""
    ).strip().lower()
    return {
        "react_max_iters": _int("QWENPAW_DATA_REACT_MAX_ITERS", 10000),
        "llm_retry_enabled": raw_retry not in {"0", "false", "off"},
        "llm_max_retries": _int("QWENPAW_DATA_LLM_MAX_RETRIES", 3),
    }


def resolve_runtime_settings(
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Per-user overrides on top of the instance defaults."""
    resolved = runtime_defaults()
    for key in RUNTIME_SETTING_KEYS:
        value = None if overrides is None else overrides.get(key)
        if value is not None:
            resolved[key] = value
    return resolved


def merge_runtime_patch(
    current: dict[str, Any] | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Apply only known keys; ``None`` values clear back to the default."""
    merged = dict(current or {})
    for key in RUNTIME_SETTING_KEYS:
        if key in patch:
            merged[key] = patch[key]
    return merged
