"""domain SPEC 00 §5.1：业务域与数据源解耦，工作空间内 domain_name 唯一。

12 实体重构后业务域不再直挂 datasource_id：域涉及哪些数据源全部由该域下的数据集
派生，缓存在只读列 ``datasource_scope``（``$$$`` 拼接），由
:mod:`semantic_config.services.domain_scope` 在数据集写操作时重算。
"""

from __future__ import annotations

import pytest
from _sc_dbutil import new_memory_session
from qwenpaw_data_auth import Principal
from semantic_config.db import now_iso
from semantic_config.errors import BadRequestError
from semantic_config.models.biz_domain import BizDomainCreate
from semantic_config.models.dataset_meta import DatasetMetaCreate
from semantic_config.repositories import biz_domain_repo as repo
from semantic_config.repositories import datasource_repo
from semantic_config.services import biz_domain_service as service
from semantic_config.services import dataset_meta_service
from semantic_config.services.domain_scope import (
    domain_ids_for_datasource,
    scope_contains,
)
from semantic_config.tables import dimension_dict, domain, metric_dict, verified_query
from sqlalchemy import insert as sa_insert
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

TID, WID = "tenant-a", "ws-a"
DS = "mysql-x"


def _principal(role: str = "tenant_admin") -> Principal:
    return Principal(
        user_id="alice",
        tenant_id=TID,
        workspace_ids=[WID],
        active_workspace_id=WID,
        role=role,
    )


async def _seed_ds(db, code: str = DS) -> None:
    await datasource_repo.insert(
        db,
        code,
        "业务库",
        "mysql",
        "{}",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )


async def _scope_of(db, domain_id: int) -> str | None:
    """读回派生列，避免测试自己拼 SQL。"""
    row = await repo.find_response_by_id(db, domain_id, TID, WID)
    return row["datasource_scope"]


async def _seed_child(db, table, domain_id: int, **cols) -> None:
    ts = now_iso()
    await db.execute(
        sa_insert(table).values(
            domain_id=domain_id,
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
            updated_by="alice",
            created_at=ts,
            updated_at=ts,
            **cols,
        )
    )


# ---------------- repo ----------------


async def test_insert_and_response_includes_datasource_scope():
    """响应列为 domain_id/domain_name/description/datasource_scope，无 datasource_id。"""
    db = await new_memory_session()
    new_id = await repo.insert(
        db,
        "电商",
        "电商分析",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["domain_id"] == new_id
    assert row["domain_name"] == "电商"
    assert row["description"] == "电商分析"
    # 新建的域尚无数据集，范围是空串而非 NULL
    assert row["datasource_scope"] == ""
    keys = set(row.keys())
    assert "datasource_id" not in keys
    assert "datasource_name" not in keys
    assert "aliases" not in keys
    await db.close()


async def test_exists_by_name_scoped_by_workspace():
    """查重键去掉 datasource 维度，只剩 (tenant, ws, domain_name)。"""
    db = await new_memory_session()
    new_id = await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert await repo.exists_by_name(db, "电商", TID, WID)
    assert not await repo.exists_by_name(db, "电商", "tenant-b", WID)
    assert not await repo.exists_by_name(db, "电商", TID, "ws-b")
    assert not await repo.exists_by_name_excluding(db, "电商", new_id, TID, WID)
    await db.close()


async def test_unique_name_enforced_per_workspace():
    """同工作空间内同名业务域冲突——不再按数据源分片。"""
    db = await new_memory_session()
    await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            "电商",
            "d2",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_same_name_allowed_across_workspaces_and_tenants():
    """唯一键仍含租户/工作空间：跨隔离面同名不冲突。"""
    db = await new_memory_session()
    await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    other_ws = await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id="ws-b",
        created_by="alice",
    )
    other_tenant = await repo.insert(
        db,
        "电商",
        "d",
        tenant_id="tenant-b",
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert other_ws > 0
    assert other_tenant > 0
    await db.close()


async def test_soft_delete_frees_unique_name():
    db = await new_memory_session()
    new_id = await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    await repo.soft_delete(
        db, new_id, tenant_id=TID, workspace_id=WID, updated_by="alice"
    )
    await db.commit()
    assert not await repo.exists_by_name(db, "电商", TID, WID)
    again = await repo.insert(
        db,
        "电商",
        "d2",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert again != new_id
    await db.close()


async def test_find_page_filters_by_name_like():
    db = await new_memory_session()
    await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await repo.insert(
        db,
        "物流",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert await repo.count(db, "电", TID, WID) == 1
    rows = await repo.find_page(db, "电", 10, 0, TID, WID)
    assert [r["domain_name"] for r in rows] == ["电商"]
    assert await repo.count(db, None, TID, WID) == 2
    await db.close()


async def test_count_children_for_delete_guard():
    """删除守卫的四类子对象：数据集 / 维度字典 / 指标字典 / 黄金查询（均查新表）。"""
    db = await new_memory_session()
    await _seed_ds(db)
    new_id = await repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    empty_id = await repo.insert(
        db,
        "物流",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await dataset_meta_service.create(
        db,
        DatasetMetaCreate(
            datasource_id=DS,
            domain_id=new_id,
            dataset_name="t1",
            description="d",
            source="proj.t1",
        ),
        _principal(),
    )
    await _seed_child(db, dimension_dict, new_id, dimension_name="地区")
    await _seed_child(db, metric_dict, new_id, metric_name="GMV")
    await _seed_child(
        db,
        verified_query,
        new_id,
        question="昨天 GMV",
        verified_sql="SELECT 1",
    )
    await db.commit()

    assert await repo.count_datasets(db, new_id, TID, WID) == 1
    assert await repo.count_dimensions(db, new_id, TID, WID) == 1
    assert await repo.count_metrics(db, new_id, TID, WID) == 1
    assert await repo.count_verified_queries(db, new_id, TID, WID) == 1

    assert await repo.count_datasets(db, empty_id, TID, WID) == 0
    assert await repo.count_dimensions(db, empty_id, TID, WID) == 0
    assert await repo.count_metrics(db, empty_id, TID, WID) == 0
    assert await repo.count_verified_queries(db, empty_id, TID, WID) == 0
    await db.close()


# ---------------- datasource_scope 派生 ----------------


async def test_datasource_scope_derived_from_datasets():
    """scope = 域下未软删数据集的 datasource_id 去重按字典序 $$$ 拼接，空集为空串。"""
    db = await new_memory_session()
    await _seed_ds(db, "mysql-b")
    await _seed_ds(db, "mysql-a")
    dom = await service.create(
        db,
        BizDomainCreate(domain_name="电商", description="d"),
        _principal(),
    )
    assert dom.datasource_scope == ""

    on_b = await dataset_meta_service.create(
        db,
        DatasetMetaCreate(
            datasource_id="mysql-b",
            domain_id=dom.domain_id,
            dataset_name="t_b",
            description="d",
            source="proj.t_b",
        ),
        _principal(),
    )
    on_a = await dataset_meta_service.create(
        db,
        DatasetMetaCreate(
            datasource_id="mysql-a",
            domain_id=dom.domain_id,
            dataset_name="t_a",
            description="d",
            source="proj.t_a",
        ),
        _principal(),
    )
    # 字典序而非插入序，便于 diff
    assert await _scope_of(db, dom.domain_id) == "mysql-a$$$mysql-b"

    await dataset_meta_service.delete(db, on_a.dataset_id, _principal())
    assert await _scope_of(db, dom.domain_id) == "mysql-b"

    await dataset_meta_service.delete(db, on_b.dataset_id, _principal())
    assert await _scope_of(db, dom.domain_id) == ""
    await db.close()


async def test_datasource_scope_dedupes_multiple_datasets_on_same_source():
    """同一数据源上的多个数据集只贡献一个 token。"""
    db = await new_memory_session()
    await _seed_ds(db, "mysql-a")
    dom = await service.create(
        db,
        BizDomainCreate(domain_name="电商", description="d"),
        _principal(),
    )
    for name in ("t1", "t2"):
        await dataset_meta_service.create(
            db,
            DatasetMetaCreate(
                datasource_id="mysql-a",
                domain_id=dom.domain_id,
                dataset_name=name,
                description="d",
                source=f"proj.{name}",
            ),
            _principal(),
        )
    assert await _scope_of(db, dom.domain_id) == "mysql-a"
    await db.close()


async def test_scope_contains_matches_whole_token_only():
    """scope_contains 按 $$$ 整段比对：mysql-3f2a 不得命中 mysql-3f2a1b9c（防裸 LIKE 前缀误匹配）。"""
    db = await new_memory_session()
    long_id = await repo.insert(
        db,
        "长编码域",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    multi_id = await repo.insert(
        db,
        "多源域",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.execute(
        sa_update(domain)
        .where(domain.c.id == long_id)
        .values(datasource_scope="mysql-3f2a1b9c")
    )
    await db.execute(
        sa_update(domain)
        .where(domain.c.id == multi_id)
        .values(datasource_scope="holo-1$$$mysql-3f2a$$$pg-9")
    )
    await db.commit()

    async def _hits(datasource_id: str) -> set[int]:
        res = await db.execute(
            select(domain.c.id).where(
                domain.c.tenant_id == TID,
                domain.c.workspace_id == WID,
                domain.c.is_deleted == 0,
                scope_contains(datasource_id),
            )
        )
        return set(res.scalars().all())

    assert await _hits("mysql-3f2a") == {multi_id}
    assert await _hits("mysql-3f2a1b9c") == {long_id}
    # 首 / 中 / 尾三个位置的 token 都要命中
    assert await _hits("holo-1") == {multi_id}
    assert await _hits("pg-9") == {multi_id}
    # 任何非整段的子串都不算命中
    assert await _hits("mysql") == set()
    assert await _hits("3f2a") == set()

    # 读侧子查询（维度/指标字典的派生 datasource_id 过滤）沿用同一条件
    res = await db.execute(
        select(domain.c.id).where(
            domain.c.id.in_(domain_ids_for_datasource("mysql-3f2a", TID, WID))
        )
    )
    assert set(res.scalars().all()) == {multi_id}
    await db.close()


# ---------------- service ----------------


async def test_service_create_does_not_require_datasource():
    """建域不再需要（也不再校验）datasource_id：域是工作空间级对象。"""
    db = await new_memory_session()
    resp = await service.create(
        db,
        BizDomainCreate(domain_name="电商", description="电商分析"),
        _principal(),
    )
    assert resp.domain_name == "电商"
    assert resp.description == "电商分析"
    assert resp.datasource_scope == ""
    assert "datasource_id" not in resp.model_dump()
    await db.close()


async def test_service_create_requires_name_and_description():
    db = await new_memory_session()
    with pytest.raises(BadRequestError):
        await service.create(db, BizDomainCreate(description="d"), _principal())
    with pytest.raises(BadRequestError):
        await service.create(db, BizDomainCreate(domain_name="电商"), _principal())
    await db.close()


async def test_service_create_duplicate_name_rejected():
    db = await new_memory_session()
    await service.create(
        db,
        BizDomainCreate(domain_name="电商", description="d"),
        _principal(),
    )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            BizDomainCreate(domain_name="电商", description="d2"),
            _principal(),
        )
    await db.close()
