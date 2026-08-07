from __future__ import annotations

from pydantic import BaseModel


class WeaveTaskSubmit(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    task_name: str | None = None
    weave_mode: str | None = None


class WeaveTaskCallback(BaseModel):
    task_id: str | None = None
    status: str | None = None
    error_msg: str | None = None


class WeaveTaskResponse(BaseModel):
    id: int
    task_id: str | None = None
    task_name: str | None = None
    datasource_id: str | None = None  # 数据源对外编码
    datasource_name: str | None = None
    weave_mode: str | None = None
    status: str | None = None
    error_msg: str | None = None
    created_at: str | None = None
