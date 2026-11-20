"""维度字典（表 ``dimension_dict``，SPEC 00 §4.1）：域级对象，彻底去掉 datasource_id。

12 实体重构：字典定义在业务域上，不再持有数据源列——数据源归属由其口径
（``dataset_dimension``）带出。列表接口的 ``datasource_id`` 变成**派生过滤**：
先取 ``datasource_scope`` 含该数据源的域集合 D，再按 ``domain_id IN D`` 过滤（§15.3）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.dimension import DimensionCreate, DimensionUpdate
from semantic_config.repositories import dimension_repo as repo
from semantic_config.services import dimension_service as service
from semantic_config.tables import dataset_dimension

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


@pytest.mark.parametrize("priority", ["high", "normal", "HIGH", "MEDIUM", "LOW"])
async def test_service_preserves_priority_on_create_and_update(priority):
    db = await new_memory_session()
    try:
        domain_id = await _seed_domain(db)
        created = await service.create(
            db,
            DimensionCreate(
                domain_id=domain_id,
                dimension_name="城市",
                dimension_type="categorical",
                priority=priority,
            ),
            _principal(),
        )
        assert created.priority == priority
        assert created.dimension_type == "categorical"
        updated = await service.update(
            db,
            created.dimension_id,
            DimensionUpdate(dimension_type="time", priority="normal"),
            _principal(),
        )
        assert updated.priority == "normal"
        assert updated.dimension_type == "time"
        with pytest.raises(BadRequestError):
            await service.update(
                db,
                created.dimension_id,
                DimensionUpdate(priority="invalid"),
                _principal(),
            )
    finally:
        await db.close()


async def test_insert_and_response_contract():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        DimensionCreate(
            domain_id=domain_id,
            dimension_name="城市",
            description="城市维度",
            synonyms="city$$$都市",
            dimension_type="categorical",
            child_dimensions="省$$$市",
            priority="HIGH",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["dimension_id"] == new_id
    assert row["domain_id"] == domain_id
    assert row["domain_name"] == "电商"
    assert row["dimension_name"] == "城市"
    assert row["synonyms"] == "city$$$都市"
    assert row["dimension_type"] == "categorical"
    assert row["child_dimensions"] == "省$$$市"
    assert row["priority"] == "HIGH"
    keys = set(row.keys())
    for retired in (
        "dimension_role",
        "enums",
        "parent_name",
        "depth",
        "is_attribution",
        "is_visible",
    ):
        assert retired not in keys, retired
    await db.close()


async def test_update_accepts_omitted_legacy_priority():
    db = await new_memory_session()
    try:
        domain_id = await _seed_domain(db)
        dimension_id = await repo.insert(
            db,
            DimensionCreate(domain_id=domain_id, dimension_name="城市", priority="50"),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )

        updated = await service.update(
            db, dimension_id, DimensionUpdate(description="新版描述"), _principal()
        )
        assert updated.description == "新版描述"
        assert updated.priority == "50"
    finally:
        await db.close()


async def test_priority_defaults_normal():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        DimensionCreate(domain_id=domain_id, dimension_name="城市"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_by_id(db, new_id, TID, WID)
    assert row["priority"] == "MEDIUM"  # server_default
    assert row["dimension_type"] is None
    await db.close()


async def test_unique_key_domain_dimension_name():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    m = DimensionCreate(domain_id=domain_id, dimension_name="城市")
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_name_in_domain(db, domain_id, "城市", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            DimensionCreate(domain_id=domain_id, dimension_name="城市"),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_delete_guard_counts_bindings_only():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        DimensionCreate(domain_id=domain_id, dimension_name="城市"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    # L5 绑定一行（expr NOT NULL）
    await db.execute(
        sa_insert(dataset_dimension).values(
            dataset_id=1,
            dimension_id=new_id,
            domain_id=domain_id,
            expr="t.city",
            value_example="北京$$$上海",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
            updated_by="alice",
            created_at="now",
            updated_at="now",
        )
    )
    await db.commit()
    assert await repo.count_bindings(db, new_id, TID, WID) == 1
    await db.close()


# ---------------- service ----------------


async def test_service_create_needs_no_datasource_but_checks_parent_domain():
    """建字典不再要求数据源；父业务域与名称仍必校验。"""
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    resp = await service.create(
        db,
        DimensionCreate(domain_id=domain_id, dimension_name="城市"),
        _principal(),
    )
    assert resp.dimension_name == "城市"
    assert not hasattr(resp, "datasource_id")
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DimensionCreate(domain_id=99999, dimension_name="x"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(db, DimensionCreate(domain_id=domain_id), _principal())
    await db.close()


async def test_service_list_page_datasource_filter_is_derived_from_scope():
    """``datasource_id`` 不再是字典自己的列，而是经域的 datasource_scope 派生（§15.3）。"""
    db = await new_memory_session()
    from semantic_config.models.dataset_meta import DatasetMetaCreate
    from semantic_config.repositories import datasource_repo
    from semantic_config.services import dataset_meta_service

    for code in ("mysql-x", "mysql-y"):
        await datasource_repo.insert(
            db,
            code,
            "库",
            "mysql",
            "{}",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
    domain_id = await _seed_domain(db)
    resp = await service.create(
        db,
        DimensionCreate(domain_id=domain_id, dimension_name="城市"),
        _principal(),
    )

    # 该域还没有任何数据集 → scope 为空 → 按数据源过滤查不到。
    empty = await service.list_page(
        db,
        domain_id,
        "城",
        1,
        10,
        _principal(),
        datasource_id="mysql-x",
    )
    assert empty.total == 0
    # 不带数据源过滤时照常可见（字典是域级对象）。
    assert (
        await service.list_page(db, domain_id, "城", 1, 10, _principal())
    ).total == 1

    # 在 mysql-x 上建一个属于该域的数据集 → scope 纳入 mysql-x → 字典入围。
    await dataset_meta_service.create(
        db,
        DatasetMetaCreate(
            datasource_id="mysql-x",
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        _principal(),
    )
    hit = await service.list_page(
        db,
        domain_id,
        "城",
        1,
        10,
        _principal(),
        datasource_id="mysql-x",
    )
    assert hit.total == 1
    # 另一个数据源不在 scope 里 → 不入围。
    miss = await service.list_page(
        db,
        domain_id,
        "城",
        1,
        10,
        _principal(),
        datasource_id="mysql-y",
    )
    assert miss.total == 0

    await db.execute(
        sa_insert(dataset_dimension).values(
            dataset_id=1,
            dimension_id=resp.dimension_id,
            domain_id=domain_id,
            expr="t.city",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
            updated_by="alice",
            created_at="now",
            updated_at="now",
        )
    )
    await db.commit()
    with pytest.raises(BadRequestError):
        await service.delete(db, resp.dimension_id, _principal())
    await db.close()
