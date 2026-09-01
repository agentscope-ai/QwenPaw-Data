# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.errors import raise_api
from qwenpaw_data.host.core.api.models.common import DatasourceOptionSchema
from qwenpaw_data.host.core.cm_client import (
    CMClientError,
    CMDatasource,
    ContextManagerClient,
)
from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["datasources"])


def get_context_manager_client() -> ContextManagerClient:
    return ContextManagerClient()


def _to_option(item: CMDatasource) -> DatasourceOptionSchema:
    datasource_type = (item.datasource_type or "").strip()
    return DatasourceOptionSchema(
        id=item.datasource_id,
        name=(item.datasource_name or "").strip() or item.datasource_id,
        status="ready",
        description=datasource_type,
    )


@router.get("/datasources")
async def list_datasources(
    _identity: Identity = Depends(get_identity),
    client: ContextManagerClient = Depends(get_context_manager_client),
) -> dict[str, Any]:
    try:
        result = await run_in_threadpool(client.list_datasources)
    except CMClientError as exc:
        logger.warning("DataBridge datasource list failed: %s", exc)
        raise_api(
            "VALIDATION",
            f"Unable to load data sources from DataBridge: {exc}",
            status=502,
        )
    return {"items": [_to_option(item) for item in result.items]}
