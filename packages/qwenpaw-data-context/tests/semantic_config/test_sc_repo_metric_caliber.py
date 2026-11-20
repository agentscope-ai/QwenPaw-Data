"""metric_caliber v2.1 实体测试（更名自 metric_formula_lib，L7）。

mgw1-storage C10。删 datasource_id/formula/date_range/formula_evidence/derived_from/
evidence_ext；新增 expr(NOT NULL，D5 可暂存 prose)/time_window(NOT NULL，key 段)/
additivity/is_default(可空，NULL=未标注)。唯一键含 time_window。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.metric_caliber import CaliberCreate, CaliberUpdate
from semantic_config.repositories import metric_caliber_repo as repo
from semantic_config.services import metric_caliber_service as service

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
    """父链：域 + 数据集 + 指标；返回 (metric_id, dataset_id)。"""
    from semantic_config.models.dataset_meta import DatasetMetaCreate
    from semantic_config.models.metric_lib import MetricCreate
    from semantic_config.repositories import (
        biz_domain_repo,
        dataset_meta_repo,
        datasource_repo,
        metric_lib_repo,
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
    metric_id = await metric_lib_repo.insert(
        db,
        MetricCreate(domain_id=domain_id, metric_name="DAU"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    return metric_id, dataset_id


# ---------------- repo ----------------


async def test_insert_and_response_contract():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    new_id = await repo.insert(
        db,
        CaliberCreate(
            metric_id=metric_id,
            dataset_id=dataset_id,
            expr="count(distinct uid)",
            time_window="1d",
            additivity="additive",
            is_default=1,
        ),
        domain_id=1,
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["id"] == new_id
    assert row["metric_id"] == metric_id
    assert row["dataset_id"] == dataset_id
    assert row["domain_id"] == 1
    assert row["metric_name"] == "DAU"
    assert row["dataset_name"] == "订单明细"
    assert row["expr"] == "count(distinct uid)"
    assert row["time_window"] == "1d"
    assert row["additivity"] == "additive"
    assert row["is_default"] == 1
    keys = set(row.keys())
    for retired in (
        "formula",
        "date_range",
        "formula_evidence",
        "derived_from",
        "evidence_ext",
    ):
        assert retired not in keys, retired
    await db.close()


async def test_is_default_nullable():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    new_id = await repo.insert(
        db,
        CaliberCreate(
            metric_id=metric_id, dataset_id=dataset_id, expr="x", time_window="7d"
        ),
        domain_id=1,
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_by_id(db, new_id, TID, WID)
    assert row["is_default"] is None  # NULL=未标注，不兜底
    await db.close()


async def test_unique_key_includes_time_window():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    m = CaliberCreate(
        metric_id=metric_id, dataset_id=dataset_id, expr="x", time_window="1d"
    )
    await repo.insert(
        db, m, domain_id=1, tenant_id=TID, workspace_id=WID, created_by="alice"
    )
    await db.commit()
    assert await repo.exists_key(db, metric_id, dataset_id, "1d", TID, WID)
    # 同 metric+dataset+window → 冲突
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            CaliberCreate(
                metric_id=metric_id, dataset_id=dataset_id, expr="y", time_window="1d"
            ),
            domain_id=1,
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    # 不同 window → 放行
    ok = await repo.insert(
        db,
        CaliberCreate(
            metric_id=metric_id, dataset_id=dataset_id, expr="y", time_window="7d"
        ),
        domain_id=1,
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.commit()
    assert ok
    await db.close()


# ---------------- service ----------------


async def test_service_create_requires_expr_time_window_parents():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            CaliberCreate(metric_id=metric_id, dataset_id=dataset_id, time_window="1d"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            CaliberCreate(metric_id=metric_id, dataset_id=dataset_id, expr="x"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            CaliberCreate(
                metric_id=99999, dataset_id=dataset_id, expr="x", time_window="1d"
            ),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            CaliberCreate(
                metric_id=metric_id, dataset_id=99999, expr="x", time_window="1d"
            ),
            _principal(),
        )
    await db.close()


async def test_service_create_ok_derives_domain_and_d5_prose_allowed():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    # D5：expr 允许暂存 prose（候选态待转 SQL），存储不校验形态
    resp = await service.create(
        db,
        CaliberCreate(
            metric_id=metric_id,
            dataset_id=dataset_id,
            expr="按天去重用户数（待转 SQL）",
            time_window="1d",
        ),
        _principal(),
    )
    assert resp.domain_id == 1  # 派生自父指标
    assert resp.metric_name == "DAU"
    await db.close()


async def test_service_update_window_conflict_and_delete_by_metric():
    db = await new_memory_session()
    metric_id, dataset_id = await _seed(db)
    a = await service.create(
        db,
        CaliberCreate(
            metric_id=metric_id, dataset_id=dataset_id, expr="x", time_window="1d"
        ),
        _principal(),
    )
    b = await service.create(
        db,
        CaliberCreate(
            metric_id=metric_id, dataset_id=dataset_id, expr="y", time_window="7d"
        ),
        _principal(),
    )
    # b 改成 1d → 与 a 冲突
    with pytest.raises(BadRequestError):
        await service.update(db, b.id, CaliberUpdate(time_window="1d"), _principal())
    # b 改成 30d → 放行
    updated = await service.update(
        db, b.id, CaliberUpdate(time_window="30d"), _principal()
    )
    assert updated.time_window == "30d"
    assert a.id
    n = await service.delete_by_metric(db, metric_id, _principal())
    assert n["message"]
    rows = await repo.find_all_by_metric(db, metric_id, TID, WID)
    assert rows == []
    await db.close()
