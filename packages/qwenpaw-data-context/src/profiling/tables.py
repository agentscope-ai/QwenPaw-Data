"""profiling 的 SQLAlchemy Core 表定义（9 张），与 semantic_config 共用同一 MetaData。

方言取舍同 semantic_config.tables：自增主键 `with_variant`、索引/唯一约束仅 SQLite 生成、
Hologres 行存注入 `WITH (orientation='row')`。取代原 `profiling/schema.sql`。

实体业务列对齐 domain / dataset / dimension_dict / metric_dict，沿用现有 staging 表名。
datasource_id / task_id / 行状态等仍是探查流程字段；实体 staging 的 datasource_id
表示本轮来源，不表示正式 Domain / Dimension / Metric 归属于该数据源。
候选允许字段未完善或同名待审行并存，不复制正式表的 NOT NULL / 业务唯一约束。
旧 staging 字段由 profiling.db.init_probe_db 升级；存量正式数据和关联 ID 的迁移另行处理。
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Integer, Table, Text, text

from semantic_config.tables import (
    _HOLO_ROW,
    _audit,
    _pk,
    _sqlite_index,
    metadata,
)

probe_stage = Table(
    "probe_stage",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'NOT_STARTED'")),
    Column("current_task_id", Text),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

probe_task = Table(
    "probe_task",
    metadata,
    _pk(),
    Column("task_id", Text, nullable=False),
    Column("datasource_id", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("progress", Integer, server_default=text("0")),
    Column("message", Text),
    Column("error_msg", Text),
    Column("heartbeat_at", Float),
    Column("executor_pod", Text),
    Column("tenant_id", Text),
    Column("workspace_id", Text),
    Column("created_by", Text),
    Column("updated_by", Text),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 正式目标：domain。member_tables 是本轮成员归属证据；datasource_scope 仅在正式面维护。
staging_biz_domain = Table(
    "staging_biz_domain",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_name", Text),
    Column("description", Text),
    Column("member_tables", Text),
    # 本轮产出方式（create 新增 / refine 完善）：随行读回给前端展示。
    # 只活在 staging —— promote 不带它，正式表没有「本轮」这个概念
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 正式目标：dataset；datasource_id 已与新表同名，domain_id 的目标引用为 domain.id。
staging_dataset = Table(
    "staging_dataset",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_id", Integer),
    Column("domain_name", Text),
    Column("dataset_name", Text),
    Column("description", Text),
    # v2.1 单指针来源：表名（P3 两段全名同形态）或 SQL 文本，内容自判（②b）
    Column("source", Text),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

staging_dataset_field = Table(
    "staging_dataset_field",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    #: 可空 —— 新探出的 dataset 尚无正式 id，以 dataset_name 回指
    #: （与 staging_dataset_dimension 同款）；promote 时再补正式 id
    Column("dataset_id", Integer),
    Column("dataset_name", Text),
    Column("field_name", Text),
    Column("data_type", Text),
    # 'Y'/'N'，镜像正式表 dataset_field 的约定（④a），promote 零转换
    Column("is_primary", Text, server_default=text("'N'")),
    Column("is_nullable", Text, server_default=text("'Y'")),
    # L3：给列补业务语义的唯一入口（原 column_name_cn 的继任者）
    Column("description", Text),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

staging_dataset_dimension = Table(
    "staging_dataset_dimension",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_id", Integer),
    Column("domain_name", Text),
    Column("dataset_id", Integer),
    Column("dataset_name", Text),
    Column("dimension_name", Text),
    # v2.1：expr 落正式表 NOT NULL；value_example $$$ 多值
    Column("expr", Text),
    Column("value_example", Text),
    # 血缘列（不 promote，但入导出列随往返保真 —— upload 是覆盖重灌，白名单外
    # 的列会被洗成 NULL）：column_role 还原时间列身份供 DICT 推 dimension_role=time；
    # column_name 还原物理列身份供 DICT 聚合/归一（expr 是口径，不是列）
    Column("column_role", Text),
    Column("column_name", Text),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 正式目标：dimension_dict；domain_id 的目标引用为 domain.id。
staging_dimension = Table(
    "staging_dimension",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_id", Integer),
    Column("domain_name", Text),
    Column("dimension_name", Text),
    Column("description", Text),
    Column("synonyms", Text, server_default=text("''")),
    Column("dimension_type", Text),
    Column("child_dimensions", Text),  # $$$ 多值，与 dimension_dict 同名
    Column("priority", Text, server_default=text("'MEDIUM'")),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

staging_metric_caliber = Table(
    "staging_metric_caliber",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_id", Integer),
    Column("domain_name", Text),
    Column("dataset_id", Integer),
    Column("dataset_name", Text),
    Column("metric_name", Text),
    # v2.1：expr+time_window 落正式表 NOT NULL，time_window 进唯一键
    Column("expr", Text),
    Column("time_window", Text),
    Column("additivity", Text),
    # 血缘列（不 promote、入导出列往返保真）：物理列身份 —— 读回时 DICT 的
    # 聚合/归一/synonyms 要的是列名，不是 expr 里的 SUM(...) 表达式
    Column("column_name", Text),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 正式目标：metric_dict；domain_id 的目标引用为 domain.id。
staging_metric = Table(
    "staging_metric",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("task_id", Text),
    Column("domain_id", Integer),
    Column("domain_name", Text),
    Column("metric_name", Text),
    Column("description", Text),
    Column("synonyms", Text),
    Column("priority", Text, server_default=text("'MEDIUM'")),
    Column("origin", Text, server_default=text("'create'")),
    Column("source_type", Text, server_default=text("'ALGO'")),
    Column("probe_status", Text, server_default=text("'PENDING'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)


# ============================ 索引（仅 SQLite） ============================
_sqlite_index(
    "uq_probe_stage", probe_stage.c.datasource_id, probe_stage.c.stage, unique=True
)
_sqlite_index("idx_probe_task_ds", probe_task.c.datasource_id, probe_task.c.stage)
_sqlite_index("uq_probe_task_id", probe_task.c.task_id, unique=True)

_sqlite_index(
    "idx_probe_stage_tenant_ws", probe_stage.c.tenant_id, probe_stage.c.workspace_id
)
_sqlite_index(
    "idx_probe_task_tenant_ws", probe_task.c.tenant_id, probe_task.c.workspace_id
)
_sqlite_index(
    "idx_staging_biz_domain_tenant_ws",
    staging_biz_domain.c.tenant_id,
    staging_biz_domain.c.workspace_id,
)
_sqlite_index(
    "idx_staging_dataset_tenant_ws",
    staging_dataset.c.tenant_id,
    staging_dataset.c.workspace_id,
)
_sqlite_index(
    "idx_staging_dataset_field_tenant_ws",
    staging_dataset_field.c.tenant_id,
    staging_dataset_field.c.workspace_id,
)
_sqlite_index(
    "idx_staging_dataset_dimension_tenant_ws",
    staging_dataset_dimension.c.tenant_id,
    staging_dataset_dimension.c.workspace_id,
)
_sqlite_index(
    "idx_staging_dimension_tenant_ws",
    staging_dimension.c.tenant_id,
    staging_dimension.c.workspace_id,
)
_sqlite_index(
    "idx_staging_metric_caliber_tenant_ws",
    staging_metric_caliber.c.tenant_id,
    staging_metric_caliber.c.workspace_id,
)
_sqlite_index(
    "idx_staging_metric_tenant_ws",
    staging_metric.c.tenant_id,
    staging_metric.c.workspace_id,
)


PROFILING_TABLE_LIST = [
    probe_stage,
    probe_task,
    staging_biz_domain,
    staging_dataset,
    staging_dataset_field,
    staging_dataset_dimension,
    staging_dimension,
    staging_metric_caliber,
    staging_metric,
]

# 名称 → Table：staging_repo 按 STAGE_DEFS 的 table 名动态取表。
PROFILING_TABLES = {t.name: t for t in PROFILING_TABLE_LIST}
