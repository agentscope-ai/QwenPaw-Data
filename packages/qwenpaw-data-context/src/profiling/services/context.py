"""ProbeContext 组装：按归属**一次取齐**全部事实（新正式实体表读取面）。

算法侧不查库、不感知租户/权限/工作空间；一切隔离在此完成（SPEC §5.3 / §6.1）。

------------------------------------------------------------------------
一个函数取数，一个 dict 交出去
------------------------------------------------------------------------
``_load_facts()`` 是**唯一**的取数函数，返回 ``{事实键: DataFrame}``，
原样放进 ``ProbeContext.facts``。键的清单由算法侧声明
（``profiling.algo.FACT_KEYS``）。

数据范围过滤分三档：

======  =====================================================  =========================
档       表                                                     过滤
======  =====================================================  =========================
域层     domain / dimension_dict / metric_dict                  仅 tenant + workspace
数据源   dataset                                                上述 + datasource_id
         table_column / staging                                 上述 + datasource_id
派生层   dataset_field / dataset_dimension / metric_caliber     经父 dataset 收窄
======  =====================================================  =========================

staging 表是 profiling 自己的，保留 datasource_id 过滤不变。
域层读取完整的 workspace 可复用目录，不按 domain.datasource_scope 收窄。
正式事实只读新实体表，不回退旧表；事实键保持不变，供现有算法契约消费。

**本层只查不整形。** 交出去的是原始行；「行长什么样才算一份快照」属于
算法侧的产出契约，随 ``snapshots/`` 演进 —— 所以本模块**不 import 算法库
的任何内部类型**，连 ``StaticProbeFactsInput`` 都不认识。唯一的富化是
SQL join：``dataset_fields`` 帧带上表式来源在 P3 ``table_column`` 里的
``sample_values``（经 ``physical_table`` 用 ``table_id`` 反解表名后匹配
``(source, field_name)``，与 weave_assembler 同键）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from profiling.provider import ProbeContext
from profiling.tables import (
    staging_biz_domain,
    staging_dataset,
    staging_dataset_dimension,
    staging_dataset_field,
    staging_dimension,
    staging_metric,
    staging_metric_caliber,
)
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
    physical_table,
    table_column,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# 取数：一个函数，一次取齐
# ===========================================================================


async def _load_facts(
    db: AsyncSession, datasource_id: str, tenant_id: str, workspace_id: str
) -> dict[str, pd.DataFrame]:
    """按 ``FACT_KEYS`` 取齐全部事实。键即算法侧与本层的全部约定。

    正式表带上 join 出来的名称列（``domain_name`` / ``dataset_name`` /
    ``dimension_name`` / ``metric_name``）—— 上游引用一律按名称传递，
    id 只在本层与 sink 之间用。
    """

    def scoped(table):
        """workspace 范围：域层实体无 datasource_id 列。"""
        return (
            table.c.tenant_id == tenant_id,
            table.c.workspace_id == workspace_id,
            table.c.is_deleted == 0,
        )

    def scoped_ds(table):
        """数据源档：按 datasource_id 收窄。"""
        return (table.c.datasource_id == datasource_id, *scoped(table))

    def scoped_dataset():
        """L2 数据集：新正式表通过 datasource_id 归属当前数据源。"""
        return (
            dataset.c.datasource_id == datasource_id,
            *scoped(dataset),
        )

    async def rows(stmt) -> pd.DataFrame:
        res = await db.execute(stmt)
        return pd.DataFrame([dict(r) for r in res.mappings().all()])

    # 派生层（dataset_field / dataset_dimension / metric_caliber）数据范围
    # 经父 dataset 收窄 —— join 新 dataset 后按其 datasource_id 过滤。
    return {
        # -- confirmed 侧：正式表 --
        "domain": await rows(select(domain).where(*scoped(domain))),
        "dataset": await rows(
            select(dataset, domain.c.domain_name.label("domain_name"))
            .select_from(dataset.outerjoin(domain, domain.c.id == dataset.c.domain_id))
            .where(*scoped_dataset())
        ),
        # L3 字段 + 表式来源的 P3 样例值富化。
        # P3 不存 table_name：经 physical_table（table_id）反解表名后匹配
        # (dataset.source, field_name) → sample_values
        "dataset_fields": await rows(
            select(
                dataset_field,
                dataset.c.dataset_name.label("dataset_name"),
                domain.c.domain_name.label("domain_name"),
                table_column.c.sample_values.label("sample_values"),
            )
            .select_from(
                dataset_field.join(dataset, dataset.c.id == dataset_field.c.dataset_id)
                .outerjoin(domain, domain.c.id == dataset_field.c.domain_id)
                .outerjoin(
                    physical_table,
                    and_(
                        physical_table.c.datasource_id == dataset.c.datasource_id,
                        physical_table.c.table_name == dataset.c.source,
                        physical_table.c.is_deleted == 0,
                    ),
                )
                .outerjoin(
                    table_column,
                    and_(
                        table_column.c.table_id == physical_table.c.id,
                        table_column.c.column_name == dataset_field.c.field_name,
                        table_column.c.is_deleted == 0,
                    ),
                )
            )
            .where(*scoped_dataset(), *scoped(dataset_field))
        ),
        "dimension_caliber": await rows(
            select(
                dataset_dimension,
                dimension_dict.c.dimension_name.label("dimension_name"),
                dataset.c.dataset_name.label("dataset_name"),
                domain.c.domain_name.label("domain_name"),
            )
            .select_from(
                dataset_dimension.join(
                    dataset, dataset.c.id == dataset_dimension.c.dataset_id
                )
                .outerjoin(
                    dimension_dict,
                    dimension_dict.c.id == dataset_dimension.c.dimension_id,
                )
                .outerjoin(domain, domain.c.id == dataset_dimension.c.domain_id)
            )
            .where(*scoped_dataset(), *scoped(dataset_dimension))
        ),
        "metric_caliber": await rows(
            select(
                metric_caliber,
                metric_dict.c.metric_name.label("metric_name"),
                dataset.c.dataset_name.label("dataset_name"),
                domain.c.domain_name.label("domain_name"),
            )
            .select_from(
                metric_caliber.join(
                    dataset, dataset.c.id == metric_caliber.c.dataset_id
                )
                .outerjoin(metric_dict, metric_dict.c.id == metric_caliber.c.metric_id)
                .outerjoin(domain, domain.c.id == metric_caliber.c.domain_id)
            )
            .where(*scoped_dataset(), *scoped(metric_caliber))
        ),
        "dimension": await rows(
            select(dimension_dict, domain.c.domain_name.label("domain_name"))
            .select_from(
                dimension_dict.outerjoin(
                    domain, domain.c.id == dimension_dict.c.domain_id
                )
            )
            .where(*scoped(dimension_dict))
        ),
        "metric": await rows(
            select(metric_dict, domain.c.domain_name.label("domain_name"))
            .select_from(
                metric_dict.outerjoin(domain, domain.c.id == metric_dict.c.domain_id)
            )
            .where(*scoped(metric_dict))
        ),
        # -- staged 侧：staging 表（名称列本来就冗余写着，无需 join）--
        "staged_domain": await rows(
            select(staging_biz_domain).where(*scoped_ds(staging_biz_domain))
        ),
        "staged_dataset": await rows(
            select(staging_dataset).where(*scoped_ds(staging_dataset))
        ),
        "staged_dataset_fields": await rows(
            select(staging_dataset_field).where(*scoped_ds(staging_dataset_field))
        ),
        "staged_dimension_caliber": await rows(
            select(staging_dataset_dimension).where(
                *scoped_ds(staging_dataset_dimension)
            )
        ),
        "staged_metric_caliber": await rows(
            select(staging_metric_caliber).where(*scoped_ds(staging_metric_caliber))
        ),
        "staged_dimension": await rows(
            select(staging_dimension).where(*scoped_ds(staging_dimension))
        ),
        "staged_metric": await rows(
            select(staging_metric).where(*scoped_ds(staging_metric))
        ),
    }


# ===========================================================================
# 装配
# ===========================================================================


async def build_context(
    db: AsyncSession,
    ds_row,
    tenant_id: str,
    workspace_id: str,
    principal: Any,
) -> ProbeContext:
    datasource_id = ds_row["datasource_id"]
    try:
        connection = json.loads(ds_row["config"]) if ds_row["config"] else {}
    except (ValueError, TypeError):
        connection = {}
    datasource_type = ds_row["datasource_type"] or ""

    facts = await _load_facts(db, datasource_id, tenant_id, workspace_id)

    return ProbeContext(
        datasource_id=datasource_id,
        datasource_type=datasource_type,
        connection=connection,
        principal=principal,
        facts=facts,
    )


__all__ = ["build_context"]
