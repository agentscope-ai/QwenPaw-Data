# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import (
    UserActiveModelRow,
    UserProviderModelRow,
    UserProviderRow,
    UserRuntimeSettingsRow,
)
from qwenpaw_data.host.core.domain.preference import (
    ModelOverride,
    ProviderCredential,
    UserPreferences,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.providers import provider_registry
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.secrets import decrypt_api_key, encrypt_api_key
from qwenpaw_data.host.core.utils.time import utcnow


class PreferencesRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    async def load(self) -> UserPreferences:
        user_id = self.user_id
        providers = {
            row.provider_id: ProviderCredential(
                api_key=decrypt_api_key(row.api_key_enc),
                base_url=row.base_url,
            )
            for row in (
                await self.db.scalars(
                    select(UserProviderRow).where(UserProviderRow.user_id == user_id)
                )
            )
        }
        models = {
            (row.provider_id, row.model_id): ModelOverride(
                source=row.source,
                name=row.name,
                thinking_enabled=row.thinking_enabled,
                generate_kwargs=row.generate_kwargs_json,
                context_size=row.context_size,
            )
            for row in (
                await self.db.scalars(
                    select(UserProviderModelRow).where(
                        UserProviderModelRow.user_id == user_id
                    )
                )
            )
        }
        active = await self.db.get(UserActiveModelRow, user_id)
        runtime = await self.db.get(UserRuntimeSettingsRow, user_id)
        return UserPreferences(
            user_id=user_id,
            providers=providers,
            models=models,
            default_provider_id=None if active is None else active.default_provider_id,
            default_model_id=None if active is None else active.default_model_id,
            light_provider_id=None if active is None else active.light_provider_id,
            light_model_id=None if active is None else active.light_model_id,
            react_max_iters=None if runtime is None else runtime.react_max_iters,
            llm_retry_enabled=None if runtime is None else runtime.llm_retry_enabled,
            llm_max_retries=None if runtime is None else runtime.llm_max_retries,
            context_trigger_ratio=(
                None if runtime is None else runtime.context_trigger_ratio
            ),
            context_reserve_ratio=(
                None if runtime is None else runtime.context_reserve_ratio
            ),
            tool_result_limit=None if runtime is None else runtime.tool_result_limit,
            tool_timeout_enabled=(
                None if runtime is None else runtime.tool_timeout_enabled
            ),
            tool_timeout_seconds=(
                None if runtime is None else runtime.tool_timeout_seconds
            ),
            tool_confirmation_level=(
                None if runtime is None else runtime.tool_confirmation_level
            ),
        )

    async def upsert_provider(self, provider_id: str, patch: dict[str, Any]) -> None:
        user_id = self.user_id
        provider_registry.require(provider_id)
        row = await self.db.get(UserProviderRow, (user_id, provider_id))
        api_key = patch.get("api_key") if "api_key" in patch else None
        if row is None and not (api_key and str(api_key).strip()):
            raise ValueError("api_key is required")
        if "base_url" in patch:
            base_url = patch["base_url"]
            base_url = None if base_url is None else (str(base_url).strip() or None)
        else:
            base_url = None if row is None else row.base_url
        # Persist the effective endpoint so Context references follow URL resets too.
        base_url = base_url or provider_registry.require(provider_id).base_url
        api_key_enc = (
            row.api_key_enc
            if api_key is None
            else encrypt_api_key(str(api_key).strip())
        )
        now = utcnow()
        if row is None:
            self.db.add(
                UserProviderRow(
                    user_id=user_id,
                    provider_id=provider_id,
                    api_key_enc=api_key_enc,
                    base_url=base_url,
                    updated_at=now,
                )
            )
        else:
            row.api_key_enc = api_key_enc
            row.base_url = base_url
            row.updated_at = now
        await self.db.flush()

    async def delete_provider(self, provider_id: str) -> None:
        user_id = self.user_id
        provider_registry.require(provider_id)
        row = await self.db.get(UserProviderRow, (user_id, provider_id))
        if row is None:
            raise LookupError(f"provider config not found: {provider_id}")
        await self.db.delete(row)
        await self.db.flush()

    async def upsert_model(
        self,
        provider_id: str,
        model_id: str,
        *,
        source: str,
        name: str | None = None,
        context_size: int | None = None,
        thinking_enabled: bool | None = None,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        user_id = self.user_id
        provider_registry.require(provider_id)
        model_id = model_id.strip()
        if not model_id:
            raise ValueError("model_id is required")
        if source == "extra" and not (name and name.strip()):
            raise ValueError("name is required for extra models")
        row = await self.db.get(UserProviderModelRow, (user_id, provider_id, model_id))
        now = utcnow()
        name = None if name is None else name.strip()
        if row is None:
            self.db.add(
                UserProviderModelRow(
                    user_id=user_id,
                    provider_id=provider_id,
                    model_id=model_id,
                    source=source,
                    name=name,
                    context_size=context_size,
                    thinking_enabled=thinking_enabled,
                    generate_kwargs_json=generate_kwargs,
                    updated_at=now,
                )
            )
        else:
            row.source = source
            row.name = name
            row.context_size = context_size
            row.thinking_enabled = thinking_enabled
            row.generate_kwargs_json = generate_kwargs
            row.updated_at = now
        await self.db.flush()

    async def delete_model(self, provider_id: str, model_id: str) -> None:
        user_id = self.user_id
        provider_registry.require(provider_id)
        row = await self.db.get(UserProviderModelRow, (user_id, provider_id, model_id))
        if row is None:
            raise LookupError(f"model config not found: {provider_id}/{model_id}")
        await self.db.delete(row)
        await self.db.flush()

    async def get_active_models(self) -> dict[str, Any]:
        user_id = self.user_id
        row = await self.db.get(UserActiveModelRow, user_id)
        if row is None:
            return {
                "default_provider_id": None,
                "default_model_id": None,
                "light_provider_id": None,
                "light_model_id": None,
            }
        return {
            "default_provider_id": row.default_provider_id,
            "default_model_id": row.default_model_id,
            "light_provider_id": row.light_provider_id,
            "light_model_id": row.light_model_id,
        }

    async def set_active_models(
        self,
        *,
        default_provider_id: str,
        default_model_id: str,
        light_provider_id: str | None = None,
        light_model_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = self.user_id
        if (light_provider_id is None) != (light_model_id is None):
            raise ValueError(
                "light_provider_id and light_model_id must be set together"
            )
        # Ensure the selection can resolve with current credentials.
        prefs = await self.load()
        prefs.default_provider_id = default_provider_id
        prefs.default_model_id = default_model_id
        prefs.light_provider_id = light_provider_id
        prefs.light_model_id = light_model_id
        # Validate against the user DB only; do not apply host env fallback.
        prefs._build()
        now = utcnow()
        row = await self.db.get(UserActiveModelRow, user_id)
        if row is None:
            self.db.add(
                UserActiveModelRow(
                    user_id=user_id,
                    default_provider_id=default_provider_id,
                    default_model_id=default_model_id,
                    light_provider_id=light_provider_id,
                    light_model_id=light_model_id,
                    updated_at=now,
                )
            )
        else:
            row.default_provider_id = default_provider_id
            row.default_model_id = default_model_id
            row.light_provider_id = light_provider_id
            row.light_model_id = light_model_id
            row.updated_at = now
        await self.db.flush()
        return await self.get_active_models()

    async def get_runtime_settings(self) -> dict[str, Any]:
        user_id = self.user_id
        row = await self.db.get(UserRuntimeSettingsRow, user_id)
        runtime = get_settings().runtime
        return {
            "react_max_iters": _or(
                None if row is None else row.react_max_iters, runtime.react_max_iters
            ),
            "llm_retry_enabled": _or(
                None if row is None else row.llm_retry_enabled,
                runtime.llm_retry_enabled,
            ),
            "llm_max_retries": _or(
                None if row is None else row.llm_max_retries, runtime.llm_max_retries
            ),
            "context_trigger_ratio": _or(
                None if row is None else row.context_trigger_ratio,
                runtime.context_trigger_ratio,
            ),
            "context_reserve_ratio": _or(
                None if row is None else row.context_reserve_ratio,
                runtime.context_reserve_ratio,
            ),
            "tool_result_limit": _or(
                None if row is None else row.tool_result_limit,
                runtime.tool_result_limit,
            ),
            "tool_timeout_enabled": _or(
                None if row is None else row.tool_timeout_enabled,
                runtime.tool_timeout_enabled,
            ),
            "tool_timeout_seconds": _or(
                None if row is None else row.tool_timeout_seconds,
                runtime.tool_timeout_seconds,
            ),
            "tool_confirmation_level": _or(
                None if row is None else row.tool_confirmation_level,
                get_settings().tool_confirmation.default_level,
            ),
        }

    async def set_runtime_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        user_id = self.user_id
        row = await self.db.get(UserRuntimeSettingsRow, user_id)
        react = None if row is None else row.react_max_iters
        retry = None if row is None else row.llm_retry_enabled
        retries = None if row is None else row.llm_max_retries
        trigger = None if row is None else row.context_trigger_ratio
        reserve = None if row is None else row.context_reserve_ratio
        tool_limit = None if row is None else row.tool_result_limit
        tool_timeout_enabled = None if row is None else row.tool_timeout_enabled
        tool_timeout = None if row is None else row.tool_timeout_seconds
        tool_confirmation_level = None if row is None else row.tool_confirmation_level
        if "react_max_iters" in patch:
            react = patch["react_max_iters"]
        if "llm_retry_enabled" in patch:
            retry = patch["llm_retry_enabled"]
        if "llm_max_retries" in patch:
            retries = patch["llm_max_retries"]
        if "context_trigger_ratio" in patch:
            trigger = patch["context_trigger_ratio"]
        if "context_reserve_ratio" in patch:
            reserve = patch["context_reserve_ratio"]
        if "tool_result_limit" in patch:
            tool_limit = patch["tool_result_limit"]
        if "tool_timeout_enabled" in patch:
            tool_timeout_enabled = patch["tool_timeout_enabled"]
        if "tool_timeout_seconds" in patch:
            tool_timeout = patch["tool_timeout_seconds"]
        if "tool_confirmation_level" in patch:
            tool_confirmation_level = patch["tool_confirmation_level"]
        runtime = get_settings().runtime
        merged_trigger = _or(trigger, runtime.context_trigger_ratio)
        merged_reserve = _or(reserve, runtime.context_reserve_ratio)
        if merged_reserve >= merged_trigger:
            raise ValueError(
                "context_reserve_ratio must be smaller than context_trigger_ratio"
            )
        now = utcnow()
        if row is None:
            self.db.add(
                UserRuntimeSettingsRow(
                    user_id=user_id,
                    react_max_iters=react,
                    llm_retry_enabled=retry,
                    llm_max_retries=retries,
                    context_trigger_ratio=trigger,
                    context_reserve_ratio=reserve,
                    tool_result_limit=tool_limit,
                    tool_timeout_enabled=tool_timeout_enabled,
                    tool_timeout_seconds=tool_timeout,
                    tool_confirmation_level=tool_confirmation_level,
                    updated_at=now,
                )
            )
        else:
            row.react_max_iters = react
            row.llm_retry_enabled = retry
            row.llm_max_retries = retries
            row.context_trigger_ratio = trigger
            row.context_reserve_ratio = reserve
            row.tool_result_limit = tool_limit
            row.tool_timeout_enabled = tool_timeout_enabled
            row.tool_timeout_seconds = tool_timeout
            row.tool_confirmation_level = tool_confirmation_level
            row.updated_at = now
        await self.db.flush()
        return await self.get_runtime_settings()


def _or(value: Any, default: Any) -> Any:
    return default if value is None else value
