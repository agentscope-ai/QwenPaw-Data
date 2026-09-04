from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query, Request

from semantic_config.db import get_db
from semantic_config.models.datasource import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    DatasourceCreate,
    DatasourceMetadataResponse,
    DatasourceResponse,
    DatasourceUpdate,
)
from semantic_config.pagination import Page
from semantic_config.services import datasource_service as service

router = APIRouter(prefix="/api/semantic-config/datasource", tags=["datasource"])
metadata_router = APIRouter(prefix="/api/v1/cm/datasources", tags=["datasource-metadata"])


@metadata_router.get("", response_model=Page[DatasourceMetadataResponse])
async def list_datasource_metadata(
    datasource_id: str | None = Query(default=None),
    datasource_name: str | None = Query(default=None),
    datasource_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    """List datasource identity/type without returning connection config."""
    return await service.list_metadata_page(
        db, datasource_id, datasource_name, datasource_type, page, size
    )


_SECRET_FIELDS = {"password", "access_key_secret", "sts_token"}


@router.get("/types")
async def list_datasource_types() -> dict:
    """支持的数据源类型及其连接表单字段（由 pydantic 配置模型单点导出）。

    前端 Add Data Source 表单据此渲染类型下拉与逐字段输入；新增类型只改
    ``datasource_config.py``，不再改前端硬编码列表。
    """
    from semantic_config.models.datasource_config import (
        CONFIG_MODEL_BY_TYPE,
        TYPE_LABELS,
    )

    items = []
    for ds_type, model in CONFIG_MODEL_BY_TYPE.items():
        schema = model.model_json_schema()
        required = set(schema.get("required", []))
        fields = []
        for name, spec in (schema.get("properties") or {}).items():
            json_type = spec.get("type")
            if json_type is None and "anyOf" in spec:
                json_type = next(
                    (opt.get("type") for opt in spec["anyOf"] if opt.get("type") not in (None, "null")),
                    "string",
                )
            fields.append(
                {
                    "name": name,
                    "type": json_type or "string",
                    "required": name in required,
                    "default": spec.get("default"),
                    "secret": name in _SECRET_FIELDS,
                }
            )
        items.append(
            {
                "type": ds_type.value,
                "label": TYPE_LABELS.get(ds_type, ds_type.value),
                "fields": fields,
            }
        )
    return {"items": items}


@router.post("", response_model=DatasourceResponse)
async def create_datasource(payload: DatasourceCreate, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.create(db, payload)


@router.get("", response_model=Page[DatasourceResponse])
async def list_datasource(
    datasource_id: str | None = Query(default=None),
    datasource_name: str | None = Query(default=None),
    datasource_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1),
    db: aiosqlite.Connection = Depends(get_db, scope="function"),
):
    return await service.list_page(db, datasource_id, datasource_name, datasource_type, page, size)


@router.post("/test-connection", response_model=ConnectionTestResponse)
async def test_connection(payload: ConnectionTestRequest):
    """存盘前测试连接：body 带 datasource_type + config。"""
    return await service.test_connection(payload.datasource_type, payload.config)


@router.get("/{datasource_id}", response_model=DatasourceResponse)
async def get_datasource(datasource_id: str, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    return await service.get_one(db, datasource_id)


@router.put("/{datasource_id}", response_model=DatasourceResponse)
async def update_datasource(
    datasource_id: str, payload: DatasourceUpdate, db: aiosqlite.Connection = Depends(get_db, scope="function")
):
    return await service.update(db, datasource_id, payload)


@router.post("/{datasource_id}/test-connection", response_model=ConnectionTestResponse)
async def test_connection_by_id(datasource_id: str, db: aiosqlite.Connection = Depends(get_db, scope="function")):
    """测试已保存数据源的连接。"""
    return await service.test_connection_by_id(db, datasource_id)


@router.delete("/{datasource_id}")
async def delete_datasource(
    datasource_id: str, request: Request, db: aiosqlite.Connection = Depends(get_db, scope="function")
):
    driver = getattr(request.app.state, "driver", None)
    return await service.delete(db, datasource_id, driver=driver)
