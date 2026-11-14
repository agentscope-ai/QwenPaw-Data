"""dataset_field v2.1 实体测试（L3 新建表：业务语义字段层）。

mgw1-storage C6。字段挂数据集；唯一键 (tenant,ws,dataset_id,field_name)；
承接自 P3 迁来的业务语义（description 等）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.dataset_field import DatasetFieldCreate
from semantic_config.repositories import dataset_field_repo as repo
from semantic_config.services import dataset_field_service as service

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


async def _seed_dataset(db) -> tuple[int, int]:
    """父数据源 + 父域 + 父数据集；返回 (dataset_id, domain_id)。"""
    from semantic_config.repositories import biz_domain_repo, datasource_repo

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
    from semantic_config.repositories import dataset_meta_repo
    from semantic_config.models.dataset_meta import DatasetMetaCreate

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
    return dataset_id, domain_id


# ---------------- repo ----------------


@pytest.mark.parametrize(
    "description_fields", [{}, {"description": None}, {"description": ""}]
)
async def test_create_allows_missing_or_empty_description(description_fields):
    db = await new_memory_session()
    try:
        dataset_id, domain_id = await _seed_dataset(db)
        result = await service.create(
            db,
            DatasetFieldCreate(
                dataset_id=dataset_id,
                field_name="sales_amount",
                **description_fields,
            ),
            _principal(),
        )
        await db.commit()
        stored = await repo.find_by_id(db, result.id, TID, WID)
        assert stored["dataset_id"] == dataset_id
        assert stored["domain_id"] == domain_id
        assert stored["description"] == description_fields.get("description")
        assert result.description == stored["description"]
    finally:
        await db.close()


async def test_insert_and_response_contract():
    db = await new_memory_session()
    dataset_id, domain_id = await _seed_dataset(db)
    new_id = await repo.insert(
        db,
        DatasetFieldCreate(
            dataset_id=dataset_id,
            domain_id=domain_id,
            field_name="订单号",
            description="订单唯一编号",
            data_type="bigint",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["id"] == new_id
    assert row["dataset_id"] == dataset_id
    assert row["domain_id"] == domain_id
    assert row["dataset_name"] == "订单明细"
    assert row["field_name"] == "订单号"
    assert row["description"] == "订单唯一编号"
    assert row["is_primary"] == "N" and row["is_nullable"] == "Y"  # server_default
    await db.close()


async def test_unique_key_dataset_field_name():
    db = await new_memory_session()
    dataset_id, domain_id = await _seed_dataset(db)
    m = DatasetFieldCreate(
        dataset_id=dataset_id,
        domain_id=domain_id,
        field_name="f1",
        description="字段描述",
    )
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_name_in_dataset(db, dataset_id, "f1", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            DatasetFieldCreate(
                dataset_id=dataset_id, field_name="f1", description="字段描述"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_exists_name_in_dataset_excluding():
    db = await new_memory_session()
    dataset_id, _ = await _seed_dataset(db)
    new_id = await repo.insert(
        db,
        DatasetFieldCreate(
            dataset_id=dataset_id, field_name="f1", description="字段描述"
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert not await repo.exists_name_in_dataset_excluding(
        db, dataset_id, "f1", new_id, TID, WID
    )
    assert await repo.exists_name_in_dataset_excluding(
        db, dataset_id, "f1", new_id + 7, TID, WID
    )
    await db.close()


async def test_find_all_by_dataset_and_page():
    db = await new_memory_session()
    dataset_id, _ = await _seed_dataset(db)
    for name in ("f1", "f2"):
        await repo.insert(
            db,
            DatasetFieldCreate(
                dataset_id=dataset_id, field_name=name, description="字段描述"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
    await db.commit()
    rows = await repo.find_all_by_dataset(db, dataset_id, TID, WID)
    assert [r["field_name"] for r in rows] == ["f1", "f2"]
    assert await repo.count(db, dataset_id, TID, WID) == 2
    page = await repo.find_page(db, dataset_id, 10, 0, TID, WID)
    assert len(page) == 2
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_field_name_and_parent_dataset():
    db = await new_memory_session()
    dataset_id, domain_id = await _seed_dataset(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetFieldCreate(dataset_id=dataset_id, domain_id=domain_id),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetFieldCreate(
                dataset_id=99999, field_name="f", description="字段描述"
            ),
            _principal(),
        )
    await db.close()


async def test_service_create_ok_and_list_by_dataset():
    db = await new_memory_session()
    dataset_id, domain_id = await _seed_dataset(db)
    resp = await service.create(
        db,
        DatasetFieldCreate(
            dataset_id=dataset_id,
            domain_id=domain_id,
            field_name="订单号",
            description="字段描述",
        ),
        _principal(),
    )
    assert resp.field_name == "订单号"
    assert resp.dataset_name == "订单明细"
    rows = await service.list_by_dataset(db, dataset_id, _principal())
    assert [r.field_name for r in rows] == ["订单号"]
    await db.close()


async def test_service_duplicate_rejected_and_update_rename():
    db = await new_memory_session()
    dataset_id, _ = await _seed_dataset(db)
    await service.create(
        db,
        DatasetFieldCreate(
            dataset_id=dataset_id, field_name="f1", description="字段描述"
        ),
        _principal(),
    )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetFieldCreate(
                dataset_id=dataset_id, field_name="f1", description="字段描述"
            ),
            _principal(),
        )
    from semantic_config.models.dataset_field import DatasetFieldUpdate

    target = await service.create(
        db,
        DatasetFieldCreate(
            dataset_id=dataset_id, field_name="f2", description="字段描述"
        ),
        _principal(),
    )
    # f2→f1 已被占用，拒绝
    with pytest.raises(BadRequestError):
        await service.update(
            db,
            target.id,
            DatasetFieldUpdate(field_name="f1"),
            _principal(),
        )
    # f2→f3 空闲，放行
    updated = await service.update(
        db,
        target.id,
        DatasetFieldUpdate(field_name="f3"),
        _principal(),
    )
    assert updated.field_name == "f3"
    await db.close()
