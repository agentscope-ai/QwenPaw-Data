"""verified_query v2.1 实体测试（L8 新建：已验证问题→SQL 对）。

mgw1-storage C11。question/verified_sql NOT NULL。唯一键 (tenant,ws,domain_id,question)。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.verified_query import VerifiedQueryCreate
from semantic_config.repositories import verified_query_repo as repo
from semantic_config.services import verified_query_service as service

from _sc_dbutil import new_memory_session

TID, WID = "tenant-a", "ws-a"


def _principal(role: str = "tenant_admin") -> Principal:
    return Principal(
        user_id="alice",
        tenant_id=TID,
        workspace_ids=[WID],
        active_workspace_id=WID,
        role=role,
    )


async def _seed_domain(db) -> int:
    from semantic_config.repositories import biz_domain_repo

    return await biz_domain_repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )


# ---------------- repo ----------------


async def test_insert_and_response_contract():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        VerifiedQueryCreate(
            domain_id=domain_id,
            question="昨天 DAU 多少？",
            verified_sql="SELECT count(distinct uid) FROM t",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["id"] == new_id
    assert row["domain_id"] == domain_id
    assert row["domain_name"] == "电商"
    assert row["question"] == "昨天 DAU 多少？"
    assert row["verified_sql"] == "SELECT count(distinct uid) FROM t"
    await db.close()


async def test_domain_id_required():
    """domain_id NOT NULL：service 层前置拦截，不落到 IntegrityError。"""
    db = await new_memory_session()
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            VerifiedQueryCreate(question="全局问题？", verified_sql="SELECT 1"),
            _principal(),
        )
    await db.close()


async def test_unique_key_domain_question():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    m = VerifiedQueryCreate(domain_id=domain_id, question="Q1", verified_sql="SELECT 1")
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_question_in_domain(db, domain_id, "Q1", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            VerifiedQueryCreate(
                domain_id=domain_id, question="Q1", verified_sql="SELECT 2"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_find_page_filters():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    for q in ("昨天 DAU", "今天 GMV"):
        await repo.insert(
            db,
            VerifiedQueryCreate(
                domain_id=domain_id, question=q, verified_sql="SELECT 1"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
    await db.commit()
    assert await repo.count(db, domain_id, "DAU", TID, WID) == 1
    rows = await repo.find_page(db, domain_id, "DAU", 10, 0, TID, WID)
    assert [r["question"] for r in rows] == ["昨天 DAU"]
    assert await repo.count(db, None, None, TID, WID) == 2
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_question_and_sql():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            VerifiedQueryCreate(domain_id=domain_id, verified_sql="SELECT 1"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            VerifiedQueryCreate(domain_id=domain_id, question="Q"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            VerifiedQueryCreate(domain_id=99999, question="Q", verified_sql="SELECT 1"),
            _principal(),
        )
    await db.close()


async def test_service_create_ok_and_duplicate_rejected():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    resp = await service.create(
        db,
        VerifiedQueryCreate(
            domain_id=domain_id,
            question="Q1",
            verified_sql="SELECT 1",
        ),
        _principal(),
    )
    assert resp.question == "Q1"
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            VerifiedQueryCreate(
                domain_id=domain_id, question="Q1", verified_sql="SELECT 2"
            ),
            _principal(),
        )
    await db.close()
