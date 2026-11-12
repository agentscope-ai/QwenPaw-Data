from __future__ import annotations

from pydantic import BaseModel


class TableColumnCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    table_name: str | None = None  # 物理表名（含库前缀，如 proj.dwd_order）
    column_name: str | None = None
    data_type: str | None = None
    column_comment: str | None = None  # v2.1 起不再必填
    sample_values: str | None = None  # $$$ 分隔样例值
    is_primary: str | None = None  # 'Y'/'N'（④a）
    is_nullable: str | None = None  # 'Y'/'N'（④a）


class TableColumnUpdate(BaseModel):
    # datasource_id/table_name 绑定不可改
    column_name: str | None = None
    data_type: str | None = None
    column_comment: str | None = None
    sample_values: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None


class TableColumnResponse(BaseModel):
    id: int
    datasource_id: str | None = None
    datasource_name: str | None = None
    table_name: str | None = None
    column_name: str | None = None
    data_type: str | None = None
    column_comment: str | None = None
    sample_values: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None
