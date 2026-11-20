from __future__ import annotations

from pydantic import BaseModel


class VerifiedQueryCreate(BaseModel):
    domain_id: int | None = None  # NOT NULL
    question: str | None = None  # NOT NULL
    verified_sql: str | None = None  # NOT NULL


class VerifiedQueryUpdate(BaseModel):
    # domain 绑定不可改
    question: str | None = None
    verified_sql: str | None = None


class VerifiedQueryResponse(BaseModel):
    id: int
    domain_id: int | None = None
    domain_name: str | None = None
    question: str | None = None
    verified_sql: str | None = None
