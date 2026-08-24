from __future__ import annotations

from pydantic import BaseModel


class DatasourceCreate(BaseModel):
    """新增数据源。datasource_id 由后端自动生成（type-uuid），无需前端传入。"""

    datasource_name: str | None = None
    datasource_type: str | None = None
    config: dict | None = None


class DatasourceUpdate(BaseModel):
    """编辑数据源。config 传了即整体替换；未传则保持不变。"""

    datasource_name: str | None = None
    datasource_type: str | None = None
    config: dict | None = None


class DatasourceResponse(BaseModel):
    datasource_id: str | None = None
    datasource_name: str | None = None
    datasource_type: str | None = None
    config: dict | None = None


class DatasourceMetadataResponse(BaseModel):
    """Credential-free datasource identity for query clients and selectors."""

    datasource_id: str | None = None
    datasource_name: str | None = None
    datasource_type: str | None = None


class ConnectionTestRequest(BaseModel):
    """存盘前测试连接：直接带类型与连接配置。"""

    datasource_type: str
    config: dict


class ConnectionTestResponse(BaseModel):
    success: bool
    message: str
    tables_found: int | None = None
    elapsed_ms: int
