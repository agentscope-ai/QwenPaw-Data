from __future__ import annotations

from pydantic import BaseModel


class ColumnCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    dataset_id: int | None = None
    column_name: str | None = None
    column_name_cn: str | None = None
    data_type: str | None = None
    column_type: str | None = None
    dimension_type: str | None = None
    column_comment: str | None = None
    column_enums: str | None = None
    column_enums_description: str | None = None
    samples: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None


class ColumnUpdate(BaseModel):
    # 不允许改 datasource/domain/dataset 绑定
    column_name: str | None = None
    column_name_cn: str | None = None
    data_type: str | None = None
    column_type: str | None = None
    dimension_type: str | None = None
    column_comment: str | None = None
    column_enums: str | None = None
    column_enums_description: str | None = None
    samples: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None


class ColumnResponse(BaseModel):
    id: int
    dataset_id: int | None = None
    dataset_name: str | None = None
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    column_name: str | None = None
    column_name_cn: str | None = None
    data_type: str | None = None
    column_type: str | None = None
    dimension_type: str | None = None
    column_comment: str | None = None
    column_enums: str | None = None
    column_enums_description: str | None = None
    samples: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None
