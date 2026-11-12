"""数据集（表 ``dataset``，SPEC 00 §5.2）：source 单列（②b），dataset_type/sql_content 退役。

12 实体重构：数据源列 ``source_datasource`` 改名 ``datasource_id``；唯一键由
「同业务域内名唯一」改为「**同数据源内**名唯一」——同一份物理数据集可被多个域引用，
而同一数据源里不允许重名（§5.2）。数据集是「域 → 数据源」的唯一桥梁，写后需重算
所属域的 ``datasource_scope``（见 test_sc_domain_scope）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.dataset_meta import DatasetMetaCreate
from semantic_config.repositories import dataset_meta_repo as repo
from semantic_config.services import dataset_meta_service as service
from semantic_config.tables import dataset_field, metric_caliber

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


async def _seed_domain_and_ds(db) -> tuple[str, int]:
    """造父数据源 + 父业务域，返回 (datasource_id, domain_id)。"""
    from semantic_config.repositories import biz_domain_repo, datasource_repo

    ds_code = "mysql-x"
    await datasource_repo.insert(
        db,
        ds_code,
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
    return ds_code, domain_id


# ---------------- repo ----------------


async def test_insert_and_response_contract():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    new_id = await repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=ds_code,
            domain_id=domain_id,
            dataset_name="订单明细",
            description="订单粒度明细",
            source="proj.dwd_order",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["dataset_id"] == new_id
    assert row["datasource_id"] == ds_code
    assert row["datasource_name"] == "业务库"
    assert row["domain_name"] == "电商"
    assert row["dataset_name"] == "订单明细"
    assert row["description"] == "订单粒度明细"
    assert row["source"] == "proj.dwd_order"
    keys = set(row.keys())
    for retired in ("dataset_comment", "dataset_type", "sql_content", "parents"):
        assert retired not in keys, retired
    await db.close()


async def test_scoped_unique_name_in_datasource():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    m = DatasetMetaCreate(
        datasource_id=ds_code,
        domain_id=domain_id,
        dataset_name="t1",
        source="proj.t1",
    )
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_name_in_datasource(db, ds_code, "t1", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            DatasetMetaCreate(
                datasource_id=ds_code,
                domain_id=domain_id,
                dataset_name="t1",
                source="proj.t2",
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_delete_guard_counts_fields_dimensions_calibers():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    new_id = await repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=ds_code,
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    # L3 字段 / L5 绑定 / L7 口径 各一行
    await db.execute(
        sa_insert(dataset_field).values(
            dataset_id=new_id,
            domain_id=domain_id,
            field_name="f1",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
            updated_by="alice",
            created_at="now",
            updated_at="now",
        )
    )
    await db.execute(
        sa_insert(metric_caliber).values(
            metric_id=None,
            dataset_id=new_id,
            domain_id=domain_id,
            expr="sum(x)",
            time_window="1d",
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
            updated_by="alice",
            created_at="now",
            updated_at="now",
        )
    )
    await db.commit()
    assert await repo.count_fields(db, new_id, TID, WID) == 1
    assert await repo.count_dimensions(db, new_id, TID, WID) == 0
    assert await repo.count_calibers(db, new_id, TID, WID) == 1
    await db.close()


async def test_count_filters_by_datasource_domain_and_name():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    await repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=ds_code,
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert await repo.count(db, ds_code, domain_id, "t", TID, WID) == 1
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_source():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetMetaCreate(
                datasource_id=ds_code,
                domain_id=domain_id,
                dataset_name="t1",
            ),
            _principal(),
        )
    await db.close()


async def test_service_create_preserves_source_and_description():
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    resp = await service.create(
        db,
        DatasetMetaCreate(
            datasource_id=ds_code,
            domain_id=domain_id,
            dataset_name="t1",
            description="desc",
            source="SELECT 1",
        ),
        _principal(),
    )
    assert resp.source == "SELECT 1"
    assert resp.description == "desc"
    await db.close()


async def test_service_create_validates_parents():
    db = await new_memory_session()
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            DatasetMetaCreate(
                datasource_id="missing",
                domain_id=999,
                dataset_name="t1",
                source="proj.t1",
            ),
            _principal(),
        )
    await db.close()


async def test_same_name_allowed_across_datasources_not_within_one():
    """唯一键从「域内」搬到「数据源内」：跨数据源可重名，同数据源内不可。"""
    db = await new_memory_session()
    ds_code, domain_id = await _seed_domain_and_ds(db)
    from semantic_config.repositories import biz_domain_repo, datasource_repo

    await datasource_repo.insert(
        db,
        "mysql-y",
        "另一个库",
        "mysql",
        "{}",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    other_domain = await biz_domain_repo.insert(
        db,
        "物流",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=ds_code,
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    # 同名 + 同数据源，但换了业务域 → 仍冲突（旧契约下这是允许的）。
    await db.commit()
    assert await repo.exists_name_in_datasource(db, ds_code, "t1", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            DatasetMetaCreate(
                datasource_id=ds_code,
                domain_id=other_domain,
                dataset_name="t1",
                source="proj.t1",
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    # 同名 + 同业务域，但换了数据源 → 放行。
    new_id = await repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id="mysql-y",
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert new_id > 0
    await db.close()
