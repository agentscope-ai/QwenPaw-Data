from __future__ import annotations

from pydantic import BaseModel, Field


class ExploreScopeUpdate(BaseModel):
    """mg-088 探查范围修改请求（§2.2 PUT；缺省字段不改）。"""

    schemas: str | None = None  # $$$ 多值 schema 白名单；空 = 全库
    include_patterns: str | None = None  # $$$ 多值表名 LIKE 白名单；空 = 不限
    exclude_patterns: str | None = None  # $$$ 多值排除（含保留 token @date_suffix）
    sample_mode: str | None = None  # on / off / auto
    sample_limit: int | None = Field(default=None, ge=1, le=100)
    table_limit: int | None = Field(default=None, ge=1, le=100000)
    enabled: int | None = Field(default=None, ge=0, le=1)
