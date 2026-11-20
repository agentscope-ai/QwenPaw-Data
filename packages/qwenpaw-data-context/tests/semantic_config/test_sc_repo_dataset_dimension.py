"""dataset_dimension v2.1 实体测试（L5：expr 必填 + value_example 新增）。

mgw1-storage C8。删 datasource_id/dimension_type/data_type；
calculate_expr→expr(NOT NULL)；新增 value_example（$$$ 多值）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError
from semantic_config.models.dataset_dimension import DatasetDimensionCreate
from semantic_config.repositories import dataset_dimension_repo as repo
from semantic_config.services import dataset_dimension_service as service

from _sc_dbutil import new_memory_session

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


async def _seed(db) -> tuple[int, int]:
    """父数据源/域/数据集 + 维度；返回 (dataset_id, dimension_id)。"""
    from semantic_config.models.dataset_meta import DatasetMetaCreate
    from semantic_config.models.dimension import DimensionCreate
    from semantic_config.repositories import (
        biz_domain_repo,
        dataset_meta_repo,
        datasource_repo,
        dimension_repo,
    )

    await datasource_repo.insert(
        db,
        DS,
        "业务库",
        "mysql",
        "{}",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    domain_id = await biz_domain_repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    dataset_id = await dataset_meta_repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=DS,
            domain_id=domain_id,
            dataset_name="订单明细",
            source="proj.dwd_order",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    dim_id = await dimension_repo.insert(
        db,
        DimensionCreate(domain_id=domain_id, dimension_name="城市"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    return dataset_id, dim_id


# ---------------- repo ----------------


async def test_insert_and_response_contract():
    db = await new_memory_session()
    dataset_id, dim_id = await _seed(db)
    new_id = await repo.insert(
        db,
        DatasetDimensionCreate(
            dataset_id=dataset_id,
            dimension_id=dim_id,
            expr="t.city_name",
            value_example="北京$$$上海",
        ),
        domain_id=1,
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["id"] == new_id
    assert row["dataset_id"] == dataset_id
    assert row["dimension_id"] == dim_id
    assert row["dataset_name"] == "订单明细"
    assert row["dimension_name"] == "城市"
    assert row["expr"] == "t.city_name"
    assert row["value_example"] == "北京$$$上海"
    keys = set(row.keys())
    for retired in ("dimension_type", "data_type", "calculate_expr"):
        assert retired not in keys, retired
    await db.close()


async def test_find_all_by_dataset_and_soft_delete_by_dataset():
    db = await new_memory_session()
    dataset_id, dim_id = await _seed(db)
    await repo.insert(
        db,
        DatasetDimensionCreate(dataset_id=dataset_id, dimension_id=dim_id, expr="t.c"),
        domain_id=1,
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    rows = await repo.find_all_by_dataset(db, dataset_id, TID, WID)
    assert len(rows) == 1
    n = await repo.soft_delete_by_dataset(
        db,
        dataset_id,
        tenant_id=TID,
        workspace_id=WID,
        updated_by="alice",
    )
    await db.commit()
    assert n == 1
    assert await repo.find_all_by_dataset(db, dataset_id, TID, WID) == []
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_expr():
    db = await new_memory_session()
    dataset_id, dim_id = await _seed(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetDimensionCreate(dataset_id=dataset_id, dimension_id=dim_id),
            _principal(),
        )
    await db.close()


async def test_service_create_derives_domain_from_parent_dataset():
    db = await new_memory_session()
    dataset_id, dim_id = await _seed(db)
    resp = await service.create(
        db,
        DatasetDimensionCreate(
            dataset_id=dataset_id,
            dimension_id=dim_id,
            expr="t.city",
        ),
        _principal(),
    )
    assert resp.expr == "t.city"
    assert resp.domain_id == 1  # 派生自父数据集
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetDimensionCreate(dataset_id=99999, dimension_id=dim_id, expr="x"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetDimensionCreate(dataset_id=dataset_id, dimension_id=99999, expr="x"),
            _principal(),
        )
    await db.close()


async def test_partial_update_preserves_value_example():
    db = await new_memory_session()
    dataset_id, dim_id = await _seed(db)
    resp = await service.create(
        db,
        DatasetDimensionCreate(
            dataset_id=dataset_id,
            dimension_id=dim_id,
            expr="t.c",
            value_example="X$$$Y",
        ),
        _principal(),
    )
    from semantic_config.models.dataset_dimension import DatasetDimensionUpdate

    # 部分更新：只改 expr，value_example 回填保留（NOT NULL 列不得被清 NULL）
    updated = await service.update(
        db,
        resp.id,
        DatasetDimensionUpdate(expr="t.c2"),
        _principal(),
    )
    assert updated.expr == "t.c2" and updated.value_example == "X$$$Y"
    await db.close()
