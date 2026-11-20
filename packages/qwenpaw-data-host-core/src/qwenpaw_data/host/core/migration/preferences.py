"""Convert persisted legacy preferences against an explicit target catalog.

No ambient model credentials, settings defaults or catalog fallbacks are read.
The eventual import plan must pin the supplied catalog/defaults and provision
the same target master key. This module only returns SQL row values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qwenpaw_data.host.core.providers.registry import ProviderRegistry

from .credentials import CredentialRewriter
from .legacy_json import MigrationError, _canonical, _component, identity_mapping
from .records import PreparedRecord, _SQL_INT_MAX


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _Provider(_Strict):
    api_key_enc: str
    base_url: str | None = None


class _Model(_Strict):
    source: Literal["extra", "override"]
    name: str | None = None
    context_size: int | None = Field(default=None, gt=0, le=_SQL_INT_MAX)
    thinking_enabled: bool | None = None
    generate_kwargs: dict[str, Any] | None = None


class _Active(_Strict):
    default_provider_id: str | None = None
    default_model_id: str | None = None
    light_provider_id: str | None = None
    light_model_id: str | None = None


class _Runtime(_Strict):
    react_max_iters: int | None = Field(default=None, ge=0, le=_SQL_INT_MAX)
    llm_retry_enabled: bool | None = None
    llm_max_retries: int | None = Field(default=None, ge=0, le=_SQL_INT_MAX)
    context_trigger_ratio: float | None = Field(default=None, gt=0, le=0.9)
    context_reserve_ratio: float | None = Field(default=None, gt=0, lt=0.9)
    tool_result_limit: int | None = Field(default=None, gt=0, le=_SQL_INT_MAX)
    tool_timeout_enabled: bool | None = None
    tool_timeout_seconds: float | None = Field(default=None, ge=1)
    tool_confirmation_level: Literal["off", "auto", "all"] | None = None


class _Preferences(_Strict):
    providers: dict[str, _Provider] = Field(default_factory=dict)
    models: dict[str, _Model] = Field(default_factory=dict)
    active: _Active | None = None
    runtime: _Runtime | None = None


def _model_id(value: str) -> str:
    # Model names can contain provider namespaces, slashes and colons. They are
    # SQL identifiers here, never workspace paths or filename components.
    if not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise MigrationError("Invalid model identifier")
    return value


def convert_preferences(
    document: Any,
    *,
    old_user_id: str,
    users: dict[str, dict[str, str]],
    credentials: CredentialRewriter,
    registry: ProviderRegistry,
    converted_at: datetime,
    target_context_trigger_ratio: float,
    target_context_reserve_ratio: float,
    model_context_sizes: dict[str, int] | None = None,
) -> tuple[PreparedRecord, ...]:
    """Preserve all stored preferences or fail without returning partial rows.

    Context-size additions are keyed by ``provider/model`` and only fill missing
    values of non-catalog extra models. Unknown/unused additions are refused, so
    a typo cannot silently leave a model unusable. Removed catalog models need
    explicit model remapping, which this converter does not guess or perform.
    """
    users = identity_mapping({"version": 1, "users": users})
    if old_user_id not in users:
        raise MigrationError("Preferences require an explicit identity mapping")
    if (
        not isinstance(converted_at, datetime)
        or converted_at.tzinfo is None
        or converted_at.utcoffset() is None
    ):
        raise MigrationError(
            "Preference conversion requires an explicit aware timestamp"
        )
    if not isinstance(registry, ProviderRegistry) or not isinstance(
        credentials, CredentialRewriter
    ):
        raise MigrationError(
            "Explicit target catalog and credential rewriter are required"
        )
    try:
        source = _Preferences.model_validate_json(
            _canonical(document), strict=True, extra="forbid"
        )
        defaults = _Runtime.model_validate(
            {
                "context_trigger_ratio": target_context_trigger_ratio,
                "context_reserve_ratio": target_context_reserve_ratio,
            },
            strict=True,
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MigrationError(
            "Invalid or unsupported preference document or target defaults"
        ) from None
    if (
        defaults.context_trigger_ratio is None
        or defaults.context_reserve_ratio is None
        or defaults.context_reserve_ratio >= defaults.context_trigger_ratio
    ):
        raise MigrationError("Target context ratios require reserve below trigger")
    if model_context_sizes is not None and not isinstance(model_context_sizes, dict):
        raise MigrationError("Context-size additions must be an object")
    sizes = {} if model_context_sizes is None else dict(model_context_sizes)
    if any(
        not isinstance(key, str)
        or type(value) is not int
        or not 0 < value <= _SQL_INT_MAX
        for key, value in sizes.items()
    ):
        raise MigrationError(
            "Context-size additions require model keys and positive integers"
        )
    common = {"user_id": users[old_user_id]["user_id"], "updated_at": converted_at}
    rows, consumed_sizes = [], set()

    def provider(pid):
        _component(pid)
        spec = registry.get(pid)
        if spec is None:
            raise MigrationError(
                "Legacy provider is unavailable in the explicit target catalog"
            )
        return spec

    for pid in source.providers:
        provider(pid)
    for key, model in sorted(source.models.items()):
        if "/" not in key:
            raise MigrationError("Legacy model key requires provider/model")
        pid, mid = key.split("/", 1)
        _model_id(mid)
        builtin = provider(pid).find_model(mid)
        if builtin is None and model.source != "extra":
            raise MigrationError(
                "Removed catalog model requires explicit model remapping"
            )
        if model.source == "extra" and not (model.name and model.name.strip()):
            raise MigrationError("Legacy extra model requires a display name")
        size, notices = model.context_size, []
        if builtin is None and size is None:
            if key not in sizes:
                raise MigrationError(
                    "Legacy extra model requires an explicit context-size addition"
                )
            size = sizes[key]
            consumed_sizes.add(key)
            notices.append("model_context_size_added")
        values = model.model_dump()
        values["context_size"] = size
        values["generate_kwargs_json"] = values.pop("generate_kwargs")
        rows.append(
            PreparedRecord(
                "user_provider_models",
                {
                    **common,
                    "provider_id": pid,
                    "model_id": mid,
                    **values,
                },
                tuple(notices),
            )
        )
    if consumed_sizes != set(sizes):
        raise MigrationError(
            "Unknown, conflicting or unused model context-size addition"
        )

    active = source.active
    if active is not None:
        for prefix in ("default", "light"):
            pid, mid = (
                getattr(active, f"{prefix}_provider_id"),
                getattr(active, f"{prefix}_model_id"),
            )
            if (pid is None) != (mid is None):
                raise MigrationError(
                    "Active provider and model must be selected together"
                )
            if pid is None:
                continue
            _model_id(mid)
            spec = provider(pid)
            if pid not in source.providers:
                raise MigrationError(
                    "Active model is missing stored provider credentials"
                )
            override = source.models.get(f"{pid}/{mid}")
            if spec.find_model(mid) is None and (
                override is None or override.source != "extra"
            ):
                raise MigrationError(
                    "Active model is unavailable; explicit model remapping required"
                )
        if active.default_provider_id is None:
            if active.light_provider_id is not None:
                raise MigrationError(
                    "Light-only model selection requires explicit reconciliation"
                )
        else:
            rows.append(
                PreparedRecord("user_active_models", {**common, **active.model_dump()})
            )
    if source.runtime is not None:
        runtime = source.runtime
        trigger = (
            runtime.context_trigger_ratio
            if runtime.context_trigger_ratio is not None
            else defaults.context_trigger_ratio
        )
        reserve = (
            runtime.context_reserve_ratio
            if runtime.context_reserve_ratio is not None
            else defaults.context_reserve_ratio
        )
        if reserve >= trigger:
            raise MigrationError(
                "Migrated context ratios conflict with explicit target defaults"
            )
        rows.append(
            PreparedRecord("user_runtime_settings", {**common, **runtime.model_dump()})
        )
    # Last: authenticate every credential only after other metadata validates.
    providers = credentials.rewrite_providers(
        {pid: value.model_dump() for pid, value in source.providers.items()}
    )
    for pid, values in sorted(providers.providers.items()):
        rows.append(
            PreparedRecord("user_providers", {**common, "provider_id": pid, **values})
        )
    return tuple(rows)
