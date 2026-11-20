from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.verified_query import (
    VerifiedQueryCreate,
    VerifiedQueryResponse,
    VerifiedQueryUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import biz_domain_repo
from semantic_config.repositories import verified_query_repo as repo


def _to_response(row: aiosqlite.Row) -> VerifiedQueryResponse:
    return VerifiedQueryResponse(**{k: row[k] for k in row.keys()})


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    return type(payload)(
        **{
            f: (getattr(payload, f) if getattr(payload, f) is not None else existing[f])
            for f in type(payload).model_fields
        }
    )


async def create(
    db: aiosqlite.Connection, payload: VerifiedQueryCreate, principal: Principal
) -> VerifiedQueryResponse:
    """v2.1（L8）：domain_id/question/verified_sql 均必填（存储层三列皆 NOT NULL）。"""
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if not payload.question:
        raise BadRequestError("question 不能为空")
    if not payload.verified_sql:
        raise BadRequestError("verified_sql 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    domain = await biz_domain_repo.find_by_id(db, payload.domain_id, tid, wid)
    if domain is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    tid, wid = domain["tenant_id"], domain["workspace_id"]
    if await repo.exists_question_in_domain(
        db, payload.domain_id, payload.question, tid, wid
    ):
        raise BadRequestError(f"已验证问题已存在: {payload.question}")
    new_id = await repo.insert(
        db,
        payload,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, new_id, tid, wid))


async def update(
    db: aiosqlite.Connection,
    vq_id: int,
    payload: VerifiedQueryUpdate,
    principal: Principal,
) -> VerifiedQueryResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, vq_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"已验证问题不存在: id={vq_id}")
    merged = _merge(payload, existing)
    if merged.question != existing["question"]:
        if await repo.exists_question_in_domain_excluding(
            db,
            existing["domain_id"],
            merged.question,
            vq_id,
            tid,
            wid,
        ):
            raise BadRequestError(f"已验证问题已存在: {merged.question}")
    await repo.update(
        db,
        vq_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, vq_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, vq_id: int, principal: Principal
) -> VerifiedQueryResponse:
    row = await repo.find_response_by_id(
        db, vq_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, domain_id, question, page, size, principal: Principal):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, domain_id, question, tid, wid)
    rows = await repo.find_page(
        db, domain_id, question, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(db: aiosqlite.Connection, vq_id: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, vq_id, tid, wid) is None:
        raise NotFoundError(f"已验证问题不存在: id={vq_id}")
    await repo.soft_delete(
        db, vq_id, tenant_id=tid, workspace_id=wid, updated_by=principal.user_id
    )
    return {"message": "deleted", "id": str(vq_id)}
