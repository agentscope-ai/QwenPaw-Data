"""指标字典（表 ``metric_dict``，SPEC 00 §4.2）：域级对象，彻底去掉 datasource_id。

与维度字典对称（见 test_sc_repo_dimension）：字典定义在业务域上，数据源归属由其
口径（``metric_formula_lib``）带出；列表接口的 ``datasource_id`` 是经域的
``datasource_scope`` 派生的过滤（§15.3）。
"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError

from semantic_config.errors import BadRequestError
from semantic_config.models.metric_lib import MetricCreate
from semantic_config.repositories import metric_lib_repo as repo
from semantic_config.services import metric_lib_service as service
from semantic_config.tables import metric_caliber

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


async def _seed_domain(db) -> int:
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
        MetricCreate(
            domain_id=domain_id,
            metric_name="DAU",
            description="日活",
            synonyms="日活跃$$$DAU",
            priority="HIGH",
        ),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_response_by_id(db, new_id, TID, WID)
    assert row["metric_id"] == new_id
    assert row["domain_id"] == domain_id
    assert row["domain_name"] == "电商"
    assert row["metric_name"] == "DAU"
    assert row["synonyms"] == "日活跃$$$DAU"
    assert row["priority"] == "HIGH"
    assert "datasource_id" not in set(row.keys())
    keys = set(row.keys())
    for retired in ("unit", "tags", "show_distribution", "is_polaris", "is_visible"):
        assert retired not in keys, retired
    await db.close()


async def test_priority_defaults_normal():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        MetricCreate(domain_id=domain_id, metric_name="DAU"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    row = await repo.find_by_id(db, new_id, TID, WID)
    assert row["priority"] == "MEDIUM"
    await db.close()


async def test_unique_key_domain_metric_name():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    m = MetricCreate(domain_id=domain_id, metric_name="DAU")
    await repo.insert(db, m, tenant_id=TID, workspace_id=WID, created_by="alice")
    await db.commit()
    assert await repo.exists_name_in_domain(db, domain_id, "DAU", TID, WID)
    with pytest.raises(IntegrityError):
        await repo.insert(
            db,
            MetricCreate(domain_id=domain_id, metric_name="DAU"),
            tenant_id=TID,
            workspace_id=WID,
            created_by="alice",
        )
        await db.commit()
    await db.rollback()
    await db.close()


async def test_delete_guard_counts_calibers():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    new_id = await repo.insert(
        db,
        MetricCreate(domain_id=domain_id, metric_name="DAU"),
        tenant_id=TID,
        workspace_id=WID,
        created_by="alice",
    )
    await db.execute(
        sa_insert(metric_caliber).values(
            metric_id=new_id,
            dataset_id=1,
            domain_id=domain_id,
            expr="count(distinct uid)",
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
    assert await repo.count_calibers(db, new_id, TID, WID) == 1
    await db.close()


# ---------------- service ----------------


async def test_service_create_needs_no_datasource_but_checks_parent_domain():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    resp = await service.create(
        db,
        MetricCreate(domain_id=domain_id, metric_name="DAU"),
        _principal(),
    )
    assert resp.metric_name == "DAU"
    assert not hasattr(resp, "datasource_id")
    assert not hasattr(resp, "is_polaris")
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            MetricCreate(domain_id=99999, metric_name="x"),
            _principal(),
        )
    with pytest.raises(BadRequestError):
        await service.create(
            db,
            MetricCreate(domain_id=domain_id),
            _principal(),
        )
    await db.close()


async def test_service_filters_by_name_and_rejects_deleting_referenced_metric():
    db = await new_memory_session()
    domain_id = await _seed_domain(db)
    resp = await service.create(
        db,
        MetricCreate(domain_id=domain_id, metric_name="DAU"),
        _principal(),
    )
    page = await service.list_page(db, domain_id, "DA", 1, 10, _principal())
    assert page.total == 1
    await db.execute(
        sa_insert(metric_caliber).values(
            metric_id=resp.metric_id,
            dataset_id=1,
            domain_id=domain_id,
            expr="count(distinct uid)",
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
    with pytest.raises(BadRequestError):
        await service.delete(db, resp.metric_id, _principal())
    await db.close()
