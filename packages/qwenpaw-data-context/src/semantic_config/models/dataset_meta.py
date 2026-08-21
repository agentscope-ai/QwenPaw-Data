from __future__ import annotations

from pydantic import BaseModel


class DatasetMetaCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    dataset_name: str | None = None
    dataset_comment: str | None = None
    dataset_type: str | None = None
    sql_content: str | None = None
    parents: str | None = None


class DatasetMetaUpdate(BaseModel):
    # 不允许改 datasource/domain 绑定
    dataset_name: str | None = None
    dataset_comment: str | None = None
    dataset_type: str | None = None
    sql_content: str | None = None
    parents: str | None = None


class DatasetMetaResponse(BaseModel):
    dataset_id: int
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    dataset_name: str | None = None
    dataset_comment: str | None = None
    dataset_type: str | None = None
    sql_content: str | None = None
    parents: str | None = None
