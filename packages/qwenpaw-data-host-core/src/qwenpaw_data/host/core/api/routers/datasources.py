# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from qwenpaw_data.host.core.api.auth_middleware import get_access_token
from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.errors import raise_api
from qwenpaw_data.host.core.api.models.common import (
    DatasetOptionSchema,
    DatasourceOptionSchema,
)
from qwenpaw_data.host.core.integrations.cm import (
    CMDatasource,
    CMClientError,
    CMLockedError,
    ContextManagerClient,
)
from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["datasources"])


def get_context_manager_client() -> ContextManagerClient:
    return ContextManagerClient(access_token=get_access_token())


def _raise_cm_list_error(exc: CMClientError, what: str) -> None:
    if isinstance(exc, CMLockedError):
        raise_api("VALIDATION", str(exc), status=423)
    raise_api(
        "VALIDATION",
        f"Unable to load {what} from DataBridge: {exc}",
        status=502,
    )


def _to_option(item: CMDatasource, *, recommended: bool) -> DatasourceOptionSchema:
    datasource_type = (item.datasource_type or "").strip()
    return DatasourceOptionSchema(
        id=item.datasource_id,
        name=(item.datasource_name or "").strip() or item.datasource_id,
        status="ready",
        description=datasource_type,
        recommended=recommended,
    )


async def require_known_datasets(
    client: ContextManagerClient,
    *,
    datasource_id: str | None,
    dataset_names: list[str] | None,
) -> list[str] | None:
    """Chat admission: a dataset focus needs a datasource that has every name."""
    if not dataset_names:
        return dataset_names
    ds = (datasource_id or "").strip()
    if not ds:
        raise_api("VALIDATION", "dataset selection requires a datasource", status=400)
    try:
        result = await run_in_threadpool(client.list_datasets, ds)
    except CMClientError as exc:
        logger.warning("DataBridge dataset validation failed: %s", exc)
        _raise_cm_list_error(exc, "datasets")
    known = {item.dataset_name for item in result.items}
    unknown = [name for name in dataset_names if name not in known]
    if unknown:
        raise_api(
            "VALIDATION",
            "unknown datasets for the selected datasource",
            status=400,
            details={"datasource_id": ds, "unknown_dataset_names": unknown},
        )
    return dataset_names


@router.get("/datasources")
async def list_datasources(
    _identity: Identity = Depends(get_identity),
    client: ContextManagerClient = Depends(get_context_manager_client),
) -> dict[str, Any]:
    try:
        result = await run_in_threadpool(client.list_datasources)
    except CMClientError as exc:
        logger.warning("DataBridge datasource list failed: %s", exc)
        _raise_cm_list_error(exc, "data sources")
    active_id: str | None = None
    try:
        active_id = await run_in_threadpool(client.get_active_datasource_id)
    except CMClientError as exc:
        logger.warning("DataBridge active datasource lookup failed: %s", exc)
    return {
        "items": [
            _to_option(item, recommended=item.datasource_id == active_id)
            for item in result.items
        ]
    }


@router.get("/datasets")
async def list_datasets(
    datasource_id: str = Query(default=""),
    _identity: Identity = Depends(get_identity),
    client: ContextManagerClient = Depends(get_context_manager_client),
) -> dict[str, Any]:
    ds = (datasource_id or "").strip()
    if not ds:
        raise_api("VALIDATION", "datasource_id is required", status=400)
    try:
        result = await run_in_threadpool(client.list_datasets, ds)
    except CMClientError as exc:
        logger.warning("DataBridge dataset list failed: %s", exc)
        _raise_cm_list_error(exc, "datasets")
    return {
        "items": [
            DatasetOptionSchema(
                id=item.dataset_name,
                name=item.dataset_name,
                description=(item.description or "").strip(),
            )
            for item in result.items
        ]
    }
