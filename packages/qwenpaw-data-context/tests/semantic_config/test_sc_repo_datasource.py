"""datasource v2.1 实体测试（P1：新增 description + 删除守卫换轨 dataset_meta）。

mgw1-storage C12。biz_domain 已无 datasource_id，删除守卫改数 dataset_meta（L2）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError
from semantic_config.models.dataset_meta import DatasetMetaCreate
from semantic_config.models.datasource import DatasourceCreate, DatasourceUpdate
from semantic_config.repositories import datasource_repo as repo
from semantic_config.services import datasource_service as service

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


def _sqlite_create(
    name: str = "本地库", description: str | None = None
) -> DatasourceCreate:
    return DatasourceCreate(
        datasource_name=name,
        datasource_type="sqlite",
        config={"path": "/tmp/sc_test.db"},
        description=description,
    )


# ---------------- repo ----------------


async def test_repo_insert_with_description():
    db = await new_memory_session()
    await repo.insert(
        db,
        "sqlite-x",
        "本地库",
        "sqlite",
        '{"path": "/tmp/x.db"}',
        description="测试用 SQLite",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_by_datasource_id(db, "sqlite-x", TID, WID)
    assert row["description"] == "测试用 SQLite"
    await db.close()


async def test_count_datasets_replaces_count_domains():
    """删除引用矩阵去掉业务域一项：域已与数据源解耦，只数数据集（§9.1）。"""
    db = await new_memory_session()
    await repo.insert(
        db,
        "sqlite-x",
        "本地库",
        "sqlite",
        "{}",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    from semantic_config.repositories import biz_domain_repo

    domain_id = await biz_domain_repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    from semantic_config.repositories import dataset_meta_repo

    await dataset_meta_repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id="sqlite-x",
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert await repo.count_datasets(db, "sqlite-x", TID, WID) == 1
    assert await repo.count_datasets(db, "sqlite-none", TID, WID) == 0
    await db.close()


# ---------------- service ----------------


async def test_service_create_and_update_description():
    db = await new_memory_session()
    resp = await service.create(
        db, _sqlite_create(description="业务主库"), _principal()
    )
    assert resp.description == "业务主库"
    assert resp.datasource_id.startswith("sqlite-")

    updated = await service.update(
        db,
        resp.datasource_id,
        DatasourceUpdate(description="改过描述"),
        _principal(),
    )
    assert updated.description == "改过描述"
    # 部分更新不丢名称
    assert updated.datasource_name == "本地库"
    await db.close()


async def test_service_create_is_idempotent_by_name_and_type():
    db = await new_memory_session()
    first = await service.create(
        db, _sqlite_create(description="业务主库"), _principal()
    )
    # 同名同类型再建：不新建，直接返回已存在的（含原描述），config 差异被忽略。
    dup = DatasourceCreate(
        datasource_name="本地库",
        datasource_type="sqlite",
        config={"path": "/tmp/other.db"},
        description="想改的描述",
    )
    again = await service.create(db, dup, _principal())
    assert again.datasource_id == first.datasource_id
    assert again.description == "业务主库"  # 复用既有，不覆盖
    assert await repo.count(db, None, "本地库", "sqlite", TID, WID) == 1
    # holo 归一到 postgresql：同名但类型不同 → 属于新数据源。
    other_type = await service.create(
        db,
        DatasourceCreate(
            datasource_name="本地库",
            datasource_type="holo",
            config={"host": "h", "database": "d", "user": "u", "password": "p"},
        ),
        _principal(),
    )
    assert other_type.datasource_id != first.datasource_id
    assert other_type.datasource_type == "postgresql"
    await db.close()


async def test_service_delete_guard_uses_datasets():
    db = await new_memory_session()
    resp = await service.create(db, _sqlite_create(), _principal())
    from semantic_config.repositories import biz_domain_repo

    domain_id = await biz_domain_repo.insert(
        db,
        "电商",
        "d",
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    from semantic_config.repositories import dataset_meta_repo

    ds_id = await dataset_meta_repo.insert(
        db,
        DatasetMetaCreate(
            datasource_id=resp.datasource_id,
            domain_id=domain_id,
            dataset_name="t1",
            source="proj.t1",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    with pytest.raises(BadRequestError) as ei:
        await service.delete(db, resp.datasource_id, _principal())
    assert "数据集" in str(ei.value)
    # 软删数据集后可删
    await dataset_meta_repo.soft_delete(
        db,
        ds_id,
        tenant_id=TID,
        workspace_id=WID,
        updated_by="alice",
    )
    await db.commit()
    out = await service.delete(db, resp.datasource_id, _principal())
    assert out["message"] == "deleted"
    await db.close()
