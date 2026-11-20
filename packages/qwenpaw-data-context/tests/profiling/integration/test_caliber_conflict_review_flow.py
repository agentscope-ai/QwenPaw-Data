"""CALIBER 冲突候选的真实待审与单选完成链路。"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from profiling.models.probe import ROW_CONFIRMED, ROW_PENDING, ROW_SKIPPED
from profiling.provider import CandidateMetricCaliber
from profiling.repositories import probe_stage_repo, staging_repo
from profiling.services import promote, row_service
from profiling.services.context import build_context
from profiling.services.sink import DbProbeSink
from profiling.stages import def_by_stage
from semantic_config.repositories import datasource_repo
from semantic_config.tables import (
    dataset,
    domain,
    metadata,
    metric_caliber,
    metric_dict,
)
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def test_conflicting_metric_calibers_are_pending_and_one_choice_completes() -> (
    None
):
    """同自然键的不同内容都可审核；确认一条、跳过一条后只落所选口径。"""

    import profiling.tables  # noqa: F401  # 注册 staging 表

    tenant_id = "tenant-caliber-review"
    workspace_id = "workspace-caliber-review"
    datasource_id = "source-a"
    principal = Principal(
        user_id="alice",
        tenant_id=tenant_id,
        workspace_ids=[workspace_id],
        active_workspace_id=workspace_id,
        role="tenant_admin",
    )
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await datasource_repo.insert(
                session,
                datasource_id,
                "source a",
                "sqlite",
                "{}",
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                created_by=principal.user_id,
            )
            await probe_stage_repo.ensure_stages(
                session,
                datasource_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                created_by=principal.user_id,
            )
            scope = dict(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                created_by=principal.user_id,
            )
            created = await session.execute(
                insert(domain).values(
                    domain_name="sales", description="销售域", **scope
                )
            )
            domain_id = int(created.inserted_primary_key[0])
            await session.execute(
                insert(dataset).values(
                    datasource_id=datasource_id,
                    domain_id=domain_id,
                    dataset_name="orders",
                    source="public.orders",
                    **scope,
                ),
            )
            await session.execute(
                insert(metric_dict).values(
                    domain_id=domain_id,
                    metric_name="销售收入",
                    description="订单销售收入",
                    **scope,
                ),
            )
            await session.commit()

            datasource = await datasource_repo.find_by_datasource_id(
                session, datasource_id, tenant_id, workspace_id
            )
            context = await build_context(
                session, datasource, tenant_id, workspace_id, principal
            )
            sink = DbProbeSink(
                session,
                context,
                datasource_id=datasource_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                created_by=principal.user_id,
                task_id="metric-conflict",
            )
            await sink.emit_metric_calibers(
                [
                    CandidateMetricCaliber(
                        domain_name="sales",
                        dataset_name="orders",
                        metric_name="销售收入",
                        expr="SUM(gross_amount)",
                        time_window="1d",
                        additivity="additive",
                        column_name="gross_amount",
                    ),
                    CandidateMetricCaliber(
                        domain_name="sales",
                        dataset_name="orders",
                        metric_name="销售收入",
                        expr="SUM(net_amount)",
                        time_window="1d",
                        additivity="additive",
                        column_name="net_amount",
                    ),
                ]
            )

            rows = await staging_repo.list_all(
                session,
                "staging_metric_caliber",
                datasource_id,
                tenant_id,
                workspace_id,
            )
            assert [row["probe_status"] for row in rows] == [
                ROW_PENDING,
                ROW_PENDING,
            ]

            stage = def_by_stage("METRIC_CALIBER")
            assert await row_service.batch_confirm(
                session, stage, datasource_id, [rows[0]["id"]], principal
            ) == {"confirmed": 1}
            assert await row_service.batch_skip(
                session, stage, datasource_id, [rows[1]["id"]], principal
            ) == {"skipped": 1}
            reviewed = await staging_repo.list_all(
                session,
                "staging_metric_caliber",
                datasource_id,
                tenant_id,
                workspace_id,
            )
            assert [row["probe_status"] for row in reviewed] == [
                ROW_CONFIRMED,
                ROW_SKIPPED,
            ]

            await promote.complete(session, datasource_id, "METRIC_CALIBER", principal)
            result = await promote.complete(
                session, datasource_id, "METRIC_MANAGE", principal
            )
            await session.commit()

            assert result["promoted_metric"] == 0
            assert result["promoted_metric_caliber"] == 1
            persisted = (
                await session.execute(
                    select(metric_caliber.c.expr, metric_caliber.c.time_window).where(
                        metric_caliber.c.tenant_id == tenant_id,
                        metric_caliber.c.workspace_id == workspace_id,
                        metric_caliber.c.is_deleted == 0,
                    )
                )
            ).all()
            assert persisted == [("SUM(gross_amount)", "1d")]
    finally:
        await engine.dispose()
