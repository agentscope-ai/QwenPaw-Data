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
