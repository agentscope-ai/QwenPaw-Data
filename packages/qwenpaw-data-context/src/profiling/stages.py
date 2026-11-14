"""阶段注册表：把各阶段的路由路径段、探查函数、staging 表、可编辑列、前置阶段、
行级能力（是否候选态/是否可人工新增）、结果过滤列集中登记，供 router/service 统一驱动。

对齐 SPEC §3.2、§6.2、§7.2、§7.3。
"""

from __future__ import annotations

from dataclasses import dataclass

from profiling.models.probe import (
    STAGE_BIZ_DOMAIN,
    STAGE_DATASET,
    STAGE_DATASET_FIELDS,
    STAGE_DIMENSION_CALIBER,
    STAGE_DIMENSION_MANAGE,
    STAGE_METRIC_CALIBER,
    STAGE_METRIC_MANAGE,
)


@dataclass(frozen=True)
class StageDef:
    stage: str  # 阶段常量，如 BIZ_DOMAIN
    path: str  # 路由路径段，如 biz-domain
    probe_fn: str  # provider 方法名，如 probe_biz_domains
    table: str  # staging 表名
    editable: tuple[str, ...]  # 人工新增/编辑可写列（member_tables 等只读列不入内）
    prereq: str | None  # 前置阶段（须 COMPLETED/SKIPPED），None 表示首阶段
    has_rows: bool  # 是否候选态：支持 confirm/skip/delete 批量
    can_add: bool  # 是否支持人工新增行
    filter_col: str | None  # results 支持的按名过滤列（管理页返回口径页跳转用）
    #: 阶段导出/上传覆盖共用的业务列。origin 在列内但不在 editable —— 算法产出的
    #: 标记，人工不改；入导出列只为往返保真（upload 是覆盖重灌，白名单外洗成 NULL）
    excel_columns: tuple[str, ...]


STAGE_DEFS: dict[str, StageDef] = {
    STAGE_BIZ_DOMAIN: StageDef(
        stage=STAGE_BIZ_DOMAIN,
        path="biz-domain",
        probe_fn="probe_biz_domains",
        table="staging_biz_domain",
        editable=("domain_name", "description"),
        prereq=None,
        has_rows=True,
        can_add=True,
        filter_col=None,
        excel_columns=(
            "domain_name",
            "description",
            "member_tables",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_DATASET: StageDef(
        stage=STAGE_DATASET,
        path="dataset",
        probe_fn="probe_datasets",
        table="staging_dataset",
        editable=(
            "domain_id",
            "domain_name",
            "dataset_name",
            "description",
            "source",
        ),
        prereq=STAGE_BIZ_DOMAIN,
        has_rows=True,
        can_add=True,
        filter_col="dataset_name",
        excel_columns=(
            "domain_id",
            "domain_name",
            "dataset_name",
            "description",
            "source",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_DATASET_FIELDS: StageDef(
        stage=STAGE_DATASET_FIELDS,
        path="dataset-fields",
        probe_fn="probe_datasets_fields",
        table="staging_dataset_field",
        editable=(
            "dataset_id",
            "dataset_name",
            "field_name",
            "data_type",
            "is_primary",
            "is_nullable",
            "description",
        ),
        prereq=STAGE_DATASET,
        has_rows=True,
        can_add=True,
        filter_col="dataset_name",
        excel_columns=(
            "dataset_id",
            "dataset_name",
            "field_name",
            "data_type",
            "is_primary",
            "is_nullable",
            "description",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_DIMENSION_CALIBER: StageDef(
        stage=STAGE_DIMENSION_CALIBER,
        path="dimension-caliber",
        probe_fn="probe_dimension_calibers",
        table="staging_dataset_dimension",
        editable=(
            "domain_id",
            "domain_name",
            "dataset_name",
            "dimension_name",
            "expr",
            "value_example",
        ),
        prereq=STAGE_DATASET_FIELDS,
        has_rows=True,
        can_add=True,
        filter_col="dimension_name",
        # domain_id/dataset_id 必须入导出列：导出→编辑→上传的往返靠它们保住
        # 域绑定与数据集绑定（upload 是覆盖重灌，白名单外的列一律洗成 NULL）；
        # column_role 同理 —— date 身份丢了 DICT 会把时间维度全判 categorical
        excel_columns=(
            "domain_id",
            "domain_name",
            "dataset_id",
            "dataset_name",
            "dimension_name",
            "expr",
            "value_example",
            "column_role",
            "column_name",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_DIMENSION_MANAGE: StageDef(
        stage=STAGE_DIMENSION_MANAGE,
        path="dimension-manage",
        probe_fn="probe_dimensions",
        table="staging_dimension",
        editable=(
            "description",
            "synonyms",
            "dimension_type",
            "child_dimensions",
            "priority",
        ),
        prereq=STAGE_DIMENSION_CALIBER,
        has_rows=False,
        can_add=False,
        filter_col="dimension_name",
        excel_columns=(
            "domain_id",
            "domain_name",
            "dimension_name",
            "description",
            "synonyms",
            "dimension_type",
            "child_dimensions",
            "priority",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_METRIC_CALIBER: StageDef(
        stage=STAGE_METRIC_CALIBER,
        path="metric-caliber",
        probe_fn="probe_metric_calibers",
        table="staging_metric_caliber",
        editable=(
            "domain_id",
            "domain_name",
            "dataset_name",
            "metric_name",
            "expr",
            "time_window",
            "additivity",
        ),
        prereq=STAGE_DIMENSION_MANAGE,
        has_rows=True,
        can_add=True,
        filter_col="metric_name",
        excel_columns=(
            "domain_id",
            "domain_name",
            "dataset_id",
            "dataset_name",
            "metric_name",
            "expr",
            "time_window",
            "additivity",
            "column_name",
            "origin",
            "probe_status",
        ),
    ),
    STAGE_METRIC_MANAGE: StageDef(
        stage=STAGE_METRIC_MANAGE,
        path="metric-manage",
        probe_fn="probe_metrics",
        table="staging_metric",
        editable=(
            "description",
            "synonyms",
            "priority",
        ),
        prereq=STAGE_METRIC_CALIBER,
        has_rows=False,
        can_add=False,
        filter_col="metric_name",
        excel_columns=(
            "domain_id",
            "domain_name",
            "metric_name",
            "description",
            "synonyms",
            "priority",
            "origin",
            "probe_status",
        ),
    ),
}

_PATH_TO_STAGE: dict[str, str] = {d.path: s for s, d in STAGE_DEFS.items()}


def def_by_stage(stage: str) -> StageDef:
    return STAGE_DEFS[stage]


def def_by_path(path: str) -> StageDef | None:
    stage = _PATH_TO_STAGE.get(path)
    return STAGE_DEFS[stage] if stage else None
