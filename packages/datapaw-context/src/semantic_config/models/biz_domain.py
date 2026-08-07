from __future__ import annotations

from pydantic import BaseModel


class BizDomainCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码, 关联 datasource.datasource_id
    domain_name: str | None = None
    display_name: str | None = None
    description: str | None = None
    aliases: str | None = None


class BizDomainUpdate(BaseModel):
    # datasource 绑定关系不允许改，只改这些
    domain_name: str | None = None
    display_name: str | None = None
    description: str | None = None
    aliases: str | None = None


class BizDomainResponse(BaseModel):
    domain_id: int
    datasource_id: str | None = None  # 数据源对外编码
    datasource_name: str | None = None
    domain_name: str | None = None
    display_name: str | None = None
    description: str | None = None
    aliases: str | None = None
