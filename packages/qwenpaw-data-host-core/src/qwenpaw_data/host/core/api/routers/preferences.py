# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import models_to_schema, providers_to_schema
from qwenpaw_data.host.core.api.models.preferences import (
    ActiveModelsSchema,
    RuntimeSettingsSchema,
    SetActiveModelsRequest,
    SetRuntimeSettingsRequest,
    UpsertProviderModelRequest,
    UpsertProviderRequest,
)
from qwenpaw_data.host.core.providers import provider_registry
from qwenpaw_data.host.core.repositories import PreferencesRepository

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("/providers")
async def list_providers(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        prefs = await PreferencesRepository(db, identity).load()
        items = providers_to_schema(prefs, provider_registry)
    except Exception as exc:
        _raise(exc)
    return {"providers": items}


@router.put("/providers/{provider_id}")
async def upsert_provider(
    provider_id: str,
    body: UpsertProviderRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        repo = PreferencesRepository(db, identity)
        await repo.upsert_provider(provider_id, body.model_dump(exclude_unset=True))
        prefs = await repo.load()
        item = next(
            p
            for p in providers_to_schema(prefs, provider_registry)
            if p.id == provider_id
        )
    except Exception as exc:
        _raise(exc)
    return {"provider": item}


@router.delete("/providers/{provider_id}")
async def clear_provider(
    provider_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await PreferencesRepository(db, identity).delete_provider(provider_id)
    except Exception as exc:
        _raise(exc)
    return {"ok": True}


@router.get("/providers/{provider_id}/models")
async def list_provider_models(
    provider_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        prefs = await PreferencesRepository(db, identity).load()
        items = models_to_schema(prefs, provider_registry, provider_id)
    except Exception as exc:
        _raise(exc)
    return {"models": items}


@router.put("/providers/{provider_id}/models/{model_id}")
async def upsert_provider_model(
    provider_id: str,
    model_id: str,
    body: UpsertProviderModelRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        repo = PreferencesRepository(db, identity)
        await repo.upsert_model(
            provider_id,
            model_id,
            **body.model_dump(),
        )
        prefs = await repo.load()
        item = next(
            m
            for m in models_to_schema(prefs, provider_registry, provider_id)
            if m.id == model_id.strip()
        )
    except Exception as exc:
        _raise(exc)
    return {"model": item}


@router.delete("/providers/{provider_id}/models/{model_id}")
async def delete_provider_model(
    provider_id: str,
    model_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await PreferencesRepository(db, identity).delete_model(provider_id, model_id)
    except Exception as exc:
        _raise(exc)
    return {"ok": True}


@router.get("/active-models")
async def get_active_models(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = await PreferencesRepository(db, identity).get_active_models()
    except Exception as exc:
        _raise(exc)
    return {"active_models": ActiveModelsSchema.model_validate(item)}


@router.put("/active-models")
async def set_active_models(
    body: SetActiveModelsRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = await PreferencesRepository(db, identity).set_active_models(
            **body.model_dump()
        )
    except Exception as exc:
        _raise(exc)
    return {"active_models": ActiveModelsSchema.model_validate(item)}


@router.get("/runtime-settings")
async def get_runtime_settings(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = await PreferencesRepository(db, identity).get_runtime_settings()
    except Exception as exc:
        _raise(exc)
    return {"runtime_settings": RuntimeSettingsSchema.model_validate(item)}


@router.put("/runtime-settings")
async def set_runtime_settings(
    body: SetRuntimeSettingsRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = await PreferencesRepository(db, identity).set_runtime_settings(
            body.model_dump(exclude_unset=True)
        )
    except Exception as exc:
        _raise(exc)
    return {"runtime_settings": RuntimeSettingsSchema.model_validate(item)}


def _raise(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise
