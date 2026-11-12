"""数据源探查总览：列当前工作空间的数据源 + 推导每个数据源的 7 阶段状态与总状态。

读工作空间级可见（SPEC §13.4）：数据源列表与阶段行均按 principal 的 tenant/workspace 过滤。
总览为批量只读，不触发懒创建；未建过阶段行的数据源各阶段按 NOT_STARTED 呈现（等价「未开始」）。
"""

from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from profiling.models.probe import (
    STAGES,
    STAGE_BIZ_DOMAIN,
    STAGE_DATASET,
    STAGE_DATASET_FIELDS,
    STAGE_DIMENSION_CALIBER,
    STAGE_DIMENSION_MANAGE,
    STAGE_METRIC_CALIBER,
    STAGE_METRIC_MANAGE,
    STATUS_NOT_STARTED,
    OverviewRecord,
    derive_overall,
)
from profiling.repositories import probe_stage_repo as stage_repo
from semantic_config.pagination import Page, offset
from semantic_config.repositories import datasource_repo as ds_repo


async def list_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    datasource_name: str | None,
    datasource_type: str | None,
    page: int,
    size: int,
    principal: Principal,
) -> Page[OverviewRecord]:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await ds_repo.count(
        db, datasource_id, datasource_name, datasource_type, tid, wid
    )
    ds_rows = await ds_repo.find_page(
        db,
        datasource_id,
        datasource_name,
        datasource_type,
        size,
        offset(page, size),
        tid,
        wid,
    )
    ds_codes = [r["datasource_id"] for r in ds_rows]
    stage_rows = await stage_repo.list_by_datasources(db, ds_codes, tid, wid)

    # {datasource_id: {stage: status}}
    status_map: dict[str, dict[str, str]] = {}
    for row in stage_rows:
        status_map.setdefault(row["datasource_id"], {})[row["stage"]] = row["status"]

    records: list[OverviewRecord] = []
    for r in ds_rows:
        code = r["datasource_id"]
        by_stage = status_map.get(code, {})

        def _st(stage: str) -> str:
            return by_stage.get(stage, STATUS_NOT_STARTED)

        records.append(
            OverviewRecord(
                datasource_id=code,
                datasource_name=r["datasource_name"],
                datasource_type=r["datasource_type"],
                overall_status=derive_overall([_st(stage) for stage in STAGES]),
                biz_domain_status=_st(STAGE_BIZ_DOMAIN),
                dataset_status=_st(STAGE_DATASET),
                dataset_fields_status=_st(STAGE_DATASET_FIELDS),
                dimension_caliber_status=_st(STAGE_DIMENSION_CALIBER),
                dimension_manage_status=_st(STAGE_DIMENSION_MANAGE),
                metric_caliber_status=_st(STAGE_METRIC_CALIBER),
                metric_manage_status=_st(STAGE_METRIC_MANAGE),
            )
        )
    return Page(records=records, total=total, page=page, size=size)
