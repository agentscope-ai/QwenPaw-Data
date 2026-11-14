from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import domain, verified_query

# L8：已验证问题→SQL 对。唯一键 (tenant,ws,domain_id,question)；domain_id → domain.id。


def _response_select():
    return select(
        verified_query.c.id,
        verified_query.c.domain_id,
        domain.c.domain_name,
        verified_query.c.question,
        verified_query.c.verified_sql,
    ).outerjoin(
        domain,
        and_(
            domain.c.id == verified_query.c.domain_id,
            domain.c.tenant_id == verified_query.c.tenant_id,
            domain.c.workspace_id == verified_query.c.workspace_id,
            domain.c.is_deleted == 0,
        ),
    )


async def insert(
    db: AsyncSession, m, *, tenant_id: str, workspace_id: str, created_by: str
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(verified_query).values(
            domain_id=m.domain_id,
            question=m.question,
            verified_sql=m.verified_sql,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            updated_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
    )
    return int(res.inserted_primary_key[0])


async def update(
    db: AsyncSession,
    vq_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(verified_query)
        .where(
            verified_query.c.id == vq_id,
            verified_query.c.tenant_id == tenant_id,
            verified_query.c.workspace_id == workspace_id,
            verified_query.c.is_deleted == 0,
        )
        .values(
            question=m.question,
            verified_sql=m.verified_sql,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, vq_id: int, tenant_id: str, workspace_id: str):
    res = await db.execute(
        select(verified_query).where(
            verified_query.c.id == vq_id,
            verified_query.c.tenant_id == tenant_id,
            verified_query.c.workspace_id == workspace_id,
            verified_query.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, vq_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        verified_query.c.is_deleted == 0,
        verified_query.c.id == vq_id,
        verified_query.c.tenant_id == tenant_id,
        verified_query.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_question_in_domain(
    db: AsyncSession,
    domain_id: int | None,
    question: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(verified_query.c.id).where(
            verified_query.c.domain_id.is_not_distinct_from(domain_id),
            verified_query.c.question == question,
            verified_query.c.tenant_id == tenant_id,
            verified_query.c.workspace_id == workspace_id,
            verified_query.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_question_in_domain_excluding(
    db: AsyncSession,
    domain_id: int | None,
    question: str,
    vq_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(verified_query.c.id).where(
            verified_query.c.domain_id.is_not_distinct_from(domain_id),
            verified_query.c.question == question,
            verified_query.c.id != vq_id,
            verified_query.c.tenant_id == tenant_id,
            verified_query.c.workspace_id == workspace_id,
            verified_query.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(domain_id, question, tenant_id, workspace_id):
    conds = [
        verified_query.c.is_deleted == 0,
        verified_query.c.tenant_id == tenant_id,
        verified_query.c.workspace_id == workspace_id,
    ]
    if domain_id is not None:
        conds.append(verified_query.c.domain_id == domain_id)
    if question:
        conds.append(verified_query.c.question.like(f"%{question}%"))
    return conds


async def count(db, domain_id, question, tenant_id, workspace_id) -> int:
    conds = _conds(domain_id, question, tenant_id, workspace_id)
    res = await db.execute(
        select(func.count()).select_from(verified_query).where(*conds)
    )
    return int(res.scalar_one())


async def find_page(db, domain_id, question, limit, off, tenant_id, workspace_id):
    conds = _conds(domain_id, question, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(verified_query.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, vq_id: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(verified_query)
        .where(
            verified_query.c.id == vq_id,
            verified_query.c.tenant_id == tenant_id,
            verified_query.c.workspace_id == workspace_id,
            verified_query.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount
