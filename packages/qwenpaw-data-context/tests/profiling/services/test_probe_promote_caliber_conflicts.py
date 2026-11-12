"""Manage complete 的口径冲突与引用预校验。"""

from __future__ import annotations

import pytest
from qwenpaw_data_auth import Principal
from profiling.models.probe import ROW_CONFIRMED
from profiling.repositories import staging_repo
from profiling.services import promote
from semantic_config.errors import BadRequestError
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
)
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

TID = "tenant-a"
WID = "workspace-a"
USER = "alice"
DS = "source-b"
DS_A = "source-a"


async def new_memory_session() -> AsyncSession:
    """Create an isolated semantic-config/profiling SQLite session."""
    import profiling.tables  # noqa: F401
    from semantic_config.tables import metadata

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


def _principal() -> Principal:
    return Principal(
        user_id=USER,
        tenant_id=TID,
        workspace_ids=[WID],
        active_workspace_id=WID,
        role="tenant_admin",
    )


async def _insert_domain(db, name: str, description: str, **scope) -> int:
    result = await db.execute(
        insert(domain).values(domain_name=name, description=description, **scope)
    )
    return int(result.inserted_primary_key[0])


async def _seed_dataset(db) -> tuple[int, int]:
    domain_id = await _insert_domain(
        db,
        "sales",
        "sales domain",
        tenant_id=TID,
        workspace_id=WID,
        created_by=USER,
    )
    dataset_id = await _insert_dataset(
        db,
        domain_id,
        datasource_id=DS,
        dataset_name="orders",
        source="public.orders",
    )
    return domain_id, dataset_id


async def _insert_dataset(
    db,
    domain_id: int,
    *,
    datasource_id: str,
    dataset_name: str,
    source: str,
) -> int:
    result = await db.execute(
        insert(dataset).values(
            datasource_id=datasource_id,
            domain_id=domain_id,
            dataset_name=dataset_name,
            source=source,
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
    )
    return int(result.inserted_primary_key[0])


def _common() -> dict:
    return {
        "datasource_id": DS,
        "tenant_id": TID,
        "workspace_id": WID,
        "created_by": USER,
        "task_id": None,
        "probe_status": ROW_CONFIRMED,
    }


async def _stage_dimension(
    db,
    domain_id: int,
    name: str,
    *,
    domain_name: str = "sales",
    dimension_type: str = "categorical",
) -> None:
    await staging_repo.insert_row(
        db,
        "staging_dimension",
        {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "dimension_name": name,
            "description": "dimension",
            "synonyms": None,
            "dimension_type": dimension_type,
        },
        **_common(),
    )


async def _stage_dimension_caliber(
    db,
    domain_id: int,
    dataset_id: int,
    name: str,
    *,
    dataset_name: str = "orders",
    domain_name: str = "sales",
    expr: str = "channel",
    value_example: str | None = "web$$$app",
) -> None:
    await staging_repo.insert_row(
        db,
        "staging_dataset_dimension",
        {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "dimension_name": name,
            "expr": expr,
            "value_example": value_example,
        },
        **_common(),
    )


async def _stage_metric(
    db, domain_id: int, name: str, *, domain_name: str = "sales"
) -> None:
    await staging_repo.insert_row(
        db,
        "staging_metric",
        {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "metric_name": name,
            "description": "metric",
            "synonyms": None,
        },
        **_common(),
    )


async def _stage_metric_caliber(
    db,
    domain_id: int,
    dataset_id: int,
    name: str,
    *,
    dataset_name: str = "orders",
    domain_name: str = "sales",
    expr: str = "SUM(amount)",
    time_window: str = "1d",
    additivity: str | None = "additive",
) -> None:
    await staging_repo.insert_row(
        db,
        "staging_metric_caliber",
        {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "metric_name": name,
            "expr": expr,
            "time_window": time_window,
            "additivity": additivity,
        },
        **_common(),
    )


async def _count(db, table) -> int:
    value = await db.scalar(select(func.count()).select_from(table))
    return int(value or 0)


async def test_dimension_binding_conflict_is_rejected_before_entity_write() -> None:
    db = await new_memory_session()
    try:
        domain_id, dataset_id = await _seed_dataset(db)
        await _stage_dimension(db, domain_id, "channel")
        await _stage_dimension_caliber(
            db, domain_id, dataset_id, "channel", value_example="web"
        )
        await _stage_dimension_caliber(
            db, domain_id, dataset_id, "channel", value_example="app"
        )
        await db.commit()

        with pytest.raises(BadRequestError) as caught:
            await promote._complete_dimension_manage(
                db, DS, "DIMENSION_MANAGE", _principal()
            )

        message = str(caught.value)
        assert "channel" in message and "orders" in message and "冲突" in message
        assert await _count(db, dimension_dict) == 0
        assert await _count(db, dataset_dimension) == 0
    finally:
        await db.close()


async def test_metric_caliber_conflict_is_rejected_before_entity_write() -> None:
    db = await new_memory_session()
    try:
        domain_id, dataset_id = await _seed_dataset(db)
        await _stage_metric(db, domain_id, "revenue")
        await _stage_metric_caliber(
            db, domain_id, dataset_id, "revenue", additivity="additive"
        )
        await _stage_metric_caliber(
            db, domain_id, dataset_id, "revenue", additivity="non_additive"
        )
        await db.commit()

        with pytest.raises(BadRequestError) as caught:
            await promote._complete_metric_manage(db, DS, "METRIC_MANAGE", _principal())

        message = str(caught.value)
        assert all(part in message for part in ("revenue", "orders", "1d", "冲突"))
        assert await _count(db, metric_dict) == 0
        assert await _count(db, metric_caliber) == 0
    finally:
        await db.close()


async def test_identical_duplicate_payloads_promote_idempotently() -> None:
    db = await new_memory_session()
    try:
        domain_id, dataset_id = await _seed_dataset(db)
        await _stage_dimension(db, domain_id, "channel")
        await _stage_metric(db, domain_id, "revenue")
        for _ in range(2):
            await _stage_dimension_caliber(db, domain_id, dataset_id, "channel")
            await _stage_metric_caliber(db, domain_id, dataset_id, "revenue")
        await db.commit()

        dim_result = await promote._complete_dimension_manage(
            db, DS, "DIMENSION_MANAGE", _principal()
        )
        metric_result = await promote._complete_metric_manage(
            db, DS, "METRIC_MANAGE", _principal()
        )
        await db.commit()

        assert dim_result["promoted_dimension"] == 1
        assert dim_result["promoted_dataset_dimension"] == 1
        assert metric_result["promoted_metric"] == 1
        assert metric_result["promoted_metric_caliber"] == 1
        assert await _count(db, dataset_dimension) == 1
        assert await _count(db, metric_caliber) == 1

        # complete 重试不重复写入已落库的同一自然键。
        dim_retry = await promote._complete_dimension_manage(
            db, DS, "DIMENSION_MANAGE", _principal()
        )
        metric_retry = await promote._complete_metric_manage(
            db, DS, "METRIC_MANAGE", _principal()
        )
        assert dim_retry["promoted_dimension"] == 0
        assert dim_retry["promoted_dataset_dimension"] == 0
        assert metric_retry["promoted_metric"] == 0
        assert metric_retry["promoted_metric_caliber"] == 0
    finally:
        await db.close()


async def test_unresolvable_manage_entities_fail_before_unrelated_entity_write() -> (
    None
):
    db = await new_memory_session()
    try:
        domain_id, dataset_id = await _seed_dataset(db)

        # 即使同轮还有可写入的实体，孤儿口径也必须在首次写入前失败。
        await _stage_dimension(db, domain_id, "valid_dimension")
        await _stage_dimension_caliber(db, domain_id, dataset_id, "ghost_dimension")
        await db.commit()
        with pytest.raises(BadRequestError, match="ghost_dimension.*无法解析维度实体"):
            await promote._complete_dimension_manage(
                db, DS, "DIMENSION_MANAGE", _principal()
            )
        assert await _count(db, dimension_dict) == 0

        await _stage_metric(db, domain_id, "valid_metric")
        await _stage_metric_caliber(db, domain_id, dataset_id, "ghost_metric")
        await db.commit()
        with pytest.raises(BadRequestError, match="ghost_metric.*无法解析指标实体"):
            await promote._complete_metric_manage(db, DS, "METRIC_MANAGE", _principal())
        assert await _count(db, metric_dict) == 0
    finally:
        await db.close()


async def test_dimension_wrong_cross_datasource_id_falls_back_to_current_source() -> (
    None
):
    db = await new_memory_session()
    try:
        domain_id = await _insert_domain(
            db,
            "sales",
            "sales domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        foreign_dataset_id = await _insert_dataset(
            db,
            domain_id,
            datasource_id=DS_A,
            dataset_name="orders_a",
            source="public.orders",
        )
        current_dataset_id = await _insert_dataset(
            db,
            domain_id,
            datasource_id=DS,
            dataset_name="orders_b",
            source="public.orders",
        )
        await _stage_dimension(db, domain_id, "channel")
        await _stage_dimension_caliber(
            db,
            domain_id,
            foreign_dataset_id,
            "channel",
            dataset_name="orders_b",
        )
        await db.commit()

        result = await promote._complete_dimension_manage(
            db, DS, "DIMENSION_MANAGE", _principal()
        )
        bound_dataset_ids = list(
            await db.scalars(select(dataset_dimension.c.dataset_id))
        )

        assert result["promoted_dataset_dimension"] == 1
        assert bound_dataset_ids == [current_dataset_id]
        assert foreign_dataset_id not in bound_dataset_ids
    finally:
        await db.close()


async def test_metric_wrong_cross_domain_id_falls_back_to_staged_domain() -> None:
    db = await new_memory_session()
    try:
        sales_id = await _insert_domain(
            db,
            "sales",
            "sales domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        marketing_id = await _insert_domain(
            db,
            "marketing",
            "marketing domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        current_dataset_id = await _insert_dataset(
            db,
            sales_id,
            datasource_id=DS,
            dataset_name="orders",
            source="public.orders",
        )
        wrong_domain_dataset_id = await _insert_dataset(
            db,
            marketing_id,
            datasource_id=DS,
            # 同源 Dataset 名称唯一，错域引用用另一个合法 Dataset 构造。
            dataset_name="marketing_orders",
            source="public.orders",
        )
        await _stage_metric(db, sales_id, "revenue")
        await _stage_metric_caliber(db, sales_id, wrong_domain_dataset_id, "revenue")
        await db.commit()

        result = await promote._complete_metric_manage(
            db, DS, "METRIC_MANAGE", _principal()
        )
        caliber_dataset_ids = list(
            await db.scalars(select(metric_caliber.c.dataset_id))
        )

        assert result["promoted_metric_caliber"] == 1
        assert caliber_dataset_ids == [current_dataset_id]
        assert wrong_domain_dataset_id not in caliber_dataset_ids
    finally:
        await db.close()


async def test_cross_datasource_dataset_id_without_local_match_is_rejected() -> None:
    db = await new_memory_session()
    try:
        domain_id = await _insert_domain(
            db,
            "sales",
            "sales domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        foreign_dataset_id = await _insert_dataset(
            db,
            domain_id,
            datasource_id=DS_A,
            dataset_name="orders_a",
            source="public.orders",
        )
        await _stage_metric(db, domain_id, "revenue")
        await _stage_metric_caliber(
            db,
            domain_id,
            foreign_dataset_id,
            "revenue",
            dataset_name="missing_local_orders",
        )
        await db.commit()

        with pytest.raises(BadRequestError) as caught:
            await promote._complete_metric_manage(db, DS, "METRIC_MANAGE", _principal())

        message = str(caught.value)
        assert all(
            part in message
            for part in (
                "revenue",
                "missing_local_orders",
                DS,
                str(foreign_dataset_id),
                "不可信",
            )
        )
        assert await _count(db, metric_dict) == 0
        assert await _count(db, metric_caliber) == 0
    finally:
        await db.close()


async def test_metric_windows_promote_as_distinct_formal_calibers() -> None:
    db = await new_memory_session()
    try:
        domain_id, dataset_id = await _seed_dataset(db)
        await _stage_metric(db, domain_id, "revenue")
        await _stage_metric_caliber(
            db, domain_id, dataset_id, "revenue", time_window="1d"
        )
        await _stage_metric_caliber(
            db, domain_id, dataset_id, "revenue", time_window="7d"
        )
        await db.commit()

        result = await promote._complete_metric_manage(
            db, DS, "METRIC_MANAGE", _principal()
        )
        windows = sorted(await db.scalars(select(metric_caliber.c.time_window)))

        assert result["promoted_metric"] == 1
        assert result["promoted_metric_caliber"] == 2
        assert windows == ["1d", "7d"]
    finally:
        await db.close()


async def test_dimension_manage_resolves_domain_by_name_and_persists_type() -> None:
    db = await new_memory_session()
    try:
        sales_id, dataset_id = await _seed_dataset(db)
        marketing_id = await _insert_domain(
            db,
            "marketing",
            "marketing domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        # 模拟导出上传后的陈旧 domain_id；canonical domain_name 才是身份。
        await _stage_dimension(
            db,
            marketing_id,
            "order_date",
            domain_name="sales",
            dimension_type="time",
        )
        await _stage_dimension_caliber(
            db,
            marketing_id,
            dataset_id,
            "order_date",
            domain_name="sales",
            expr="created_at",
        )
        await db.commit()

        result = await promote._complete_dimension_manage(
            db, DS, "DIMENSION_MANAGE", _principal()
        )
        created = (
            (
                await db.execute(
                    select(
                        dimension_dict.c.domain_id, dimension_dict.c.dimension_type
                    ).where(dimension_dict.c.dimension_name == "order_date")
                )
            )
            .mappings()
            .one()
        )
        binding_domain = await db.scalar(select(dataset_dimension.c.domain_id))

        assert result["promoted_dimension"] == 1
        assert dict(created) == {"domain_id": sales_id, "dimension_type": "time"}
        assert binding_domain == sales_id
    finally:
        await db.close()


async def test_unknown_canonical_domain_is_rejected_before_entity_write() -> None:
    db = await new_memory_session()
    try:
        domain_id, _dataset_id = await _seed_dataset(db)
        await _stage_dimension(db, domain_id, "channel", domain_name="missing_domain")
        await db.commit()

        with pytest.raises(BadRequestError, match="missing_domain.*不存在"):
            await promote._complete_dimension_manage(
                db, DS, "DIMENSION_MANAGE", _principal()
            )

        assert await _count(db, dimension_dict) == 0
    finally:
        await db.close()


async def test_metric_manage_resolves_domain_by_name() -> None:
    db = await new_memory_session()
    try:
        sales_id, dataset_id = await _seed_dataset(db)
        marketing_id = await _insert_domain(
            db,
            "marketing",
            "marketing domain",
            tenant_id=TID,
            workspace_id=WID,
            created_by=USER,
        )
        await _stage_metric(db, marketing_id, "revenue", domain_name="sales")
        await _stage_metric_caliber(
            db,
            marketing_id,
            dataset_id,
            "revenue",
            domain_name="sales",
        )
        await db.commit()

        result = await promote._complete_metric_manage(
            db, DS, "METRIC_MANAGE", _principal()
        )
        metric_domain = await db.scalar(
            select(metric_dict.c.domain_id).where(
                metric_dict.c.metric_name == "revenue"
            )
        )
        caliber_domain = await db.scalar(select(metric_caliber.c.domain_id))

        assert result["promoted_metric"] == 1
        assert metric_domain == sales_id
        assert caliber_domain == sales_id
    finally:
        await db.close()
