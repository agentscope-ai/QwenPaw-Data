"""table_column v2.1 实体测试（更名自 dataset_column_meta，③a）。

mgw1-storage C5。key=(datasource_id, table_name, column_name)；删 dataset_id/domain_id；
samples→sample_values；业务语义迁 L3 dataset_field（C6）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.table_column import TableColumnCreate
from semantic_config.repositories import table_column_repo as repo
from semantic_config.services import table_column_service as service

from _sc_dbutil import new_memory_session, seed_physical_table

TID, WID = "tenant-a", "ws-a"
DS = "mysql-x"


async def _seed_table(db, table_name: str) -> int:
    """物理层：table_column 落 table_id，插列前先声明 physical_table（NOT NULL 外键）。"""
    return await seed_physical_table(db, DS, table_name, TID, WID)


def _principal(role: str = "tenant_admin") -> Principal:
    return Principal(
        user_id="alice",
        tenant_id=TID,
        workspace_ids=[WID],
        active_workspace_id=WID,
        role=role,
    )


async def _seed_ds(db) -> None:
    from semantic_config.repositories import datasource_repo

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


# ---------------- repo ----------------


async def test_insert_and_response_contract():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "proj.dwd_order")
    new_id = await repo.insert(
        db,
        TableColumnCreate(
            datasource_id=DS,
            table_name="proj.dwd_order",
            column_name="order_id",
            data_type="bigint",
            column_comment="订单号",
            sample_values="1$$$2",
            is_primary="Y",
            is_nullable="N",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["id"] == new_id
    assert row["datasource_id"] == DS
    assert row["datasource_name"] == "业务库"
    assert row["table_name"] == "proj.dwd_order"
    assert row["column_name"] == "order_id"
    assert row["sample_values"] == "1$$$2"
    assert row["is_primary"] == "Y" and row["is_nullable"] == "N"
    keys = set(row.keys())
    for retired in (
        "dataset_id",
        "domain_id",
        "samples",
        "column_enums",
        "column_type",
    ):
        assert retired not in keys, retired
    await db.close()


async def test_insert_requires_declared_physical_table():
    """table_id NOT NULL：physical_table 未声明 → 硬失败（不自动建表，name→id 边界解析）。"""
    db = await new_memory_session()
    await _seed_ds(db)
    with pytest.raises(BadRequestError):
        await repo.insert(
            db,
            TableColumnCreate(
                datasource_id=DS,
                table_name="未声明的表",
                column_name="c",
                data_type="int",
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
    await db.close()


async def test_boolean_defaults_yn():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "t")
    new_id = await repo.insert(
        db,
        TableColumnCreate(
            datasource_id=DS, table_name="t", column_name="c", data_type="int"
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_by_id(db, new_id, TID, WID)
    assert row["is_primary"] == "N"  # ④a server_default
    assert row["is_nullable"] == "Y"
    await db.close()


async def test_unique_key_ds_table_column():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "t1")
    await _seed_table(db, "t2")
    m = TableColumnCreate(
        datasource_id=DS, table_name="t1", column_name="c1", data_type="int"
    )
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_name_in_table(db, DS, "t1", "c1", TID, WID)
    # 同表同列 → 冲突
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            TableColumnCreate(
                datasource_id=DS, table_name="t1", column_name="c1", data_type="int"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    # 不同表同名列 → 允许
    ok = await repo.insert(
        db,
        TableColumnCreate(
            datasource_id=DS, table_name="t2", column_name="c1", data_type="int"
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert ok
    await db.close()


async def test_exists_name_in_table_excluding():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "t1")
    new_id = await repo.insert(
        db,
        TableColumnCreate(
            datasource_id=DS, table_name="t1", column_name="c1", data_type="int"
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert not await repo.exists_name_in_table_excluding(
        db, DS, "t1", "c1", new_id, TID, WID
    )
    assert await repo.exists_name_in_table_excluding(
        db, DS, "t1", "c1", new_id + 99, TID, WID
    )
    await db.close()


async def test_find_all_by_table_and_page_filter():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "t1")
    await _seed_table(db, "t2")
    for col in ("a", "b"):
        await repo.insert(
            db,
            TableColumnCreate(
                datasource_id=DS, table_name="t1", column_name=col, data_type="int"
            ),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
    await repo.insert(
        db,
        TableColumnCreate(
            datasource_id=DS, table_name="t2", column_name="x", data_type="int"
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    rows = await repo.find_all_by_table(db, DS, "t1", TID, WID)
    assert [r["column_name"] for r in rows] == ["a", "b"]
    assert await repo.count(db, DS, "t1", TID, WID) == 2
    assert await repo.count(db, DS, None, TID, WID) == 3
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_table_and_column_and_parent_ds():
    db = await new_memory_session()
    await _seed_ds(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            TableColumnCreate(datasource_id=DS, column_name="c"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            TableColumnCreate(datasource_id=DS, table_name="t"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            TableColumnCreate(datasource_id="missing", table_name="t", column_name="c"),
            _principal(),
        )
    await db.close()


async def test_service_create_ok_without_column_comment_and_list_by_table():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "proj.t1")
    resp = await service.create(
        db,
        TableColumnCreate(
            datasource_id=DS, table_name="proj.t1", column_name="c1", data_type="int"
        ),
        _principal(),
    )
    assert resp.column_name == "c1"
    assert resp.table_name == "proj.t1"
    rows = await service.list_by_table(db, DS, "proj.t1", _principal())
    assert [r.column_name for r in rows] == ["c1"]
    await db.close()


async def test_service_create_duplicate_rejected():
    db = await new_memory_session()
    await _seed_ds(db)
    await _seed_table(db, "t1")
    payload = TableColumnCreate(
        datasource_id=DS, table_name="t1", column_name="c1", data_type="int"
    )
    await service.create(db, payload, _principal())
    with pytest.raises(BadRequestError):
        await service.create(db, payload.model_copy(), _principal())
    await db.close()
