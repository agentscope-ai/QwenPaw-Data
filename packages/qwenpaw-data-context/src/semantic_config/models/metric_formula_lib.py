from __future__ import annotations

from pydantic import BaseModel


class FormulaCreate(BaseModel):
    metric_id: int | None = None
    dataset_id: int | None = None
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    formula: str | None = None
    date_range: str | None = None
    formula_evidence: str | None = None
    derived_from: str | None = None
    evidence_ext: str | None = None


class FormulaUpdate(BaseModel):
    formula: str | None = None
    date_range: str | None = None
    formula_evidence: str | None = None
    derived_from: str | None = None
    evidence_ext: str | None = None


class FormulaResponse(BaseModel):
    id: int
    metric_id: int | None = None
    metric_name: str | None = None
    dataset_id: int | None = None
    dataset_name: str | None = None
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    formula: str | None = None
    date_range: str | None = None
    formula_evidence: str | None = None
    derived_from: str | None = None
    evidence_ext: str | None = None
