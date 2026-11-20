from __future__ import annotations

from pydantic import BaseModel


class BizDomainCreate(BaseModel):
    domain_name: str | None = None
    description: str | None = None


class BizDomainUpdate(BaseModel):
    domain_name: str | None = None
    description: str | None = None


class BizDomainResponse(BaseModel):
    domain_id: int
    domain_name: str | None = None
    description: str | None = None
    #: 派生只读：该域下数据集的数据源编码去重（$$$ 分隔）。后端/Excel 导出消费，
    #: 语义配置管理前端不渲染（SPEC 00 §4.3）。
    datasource_scope: str | None = None
