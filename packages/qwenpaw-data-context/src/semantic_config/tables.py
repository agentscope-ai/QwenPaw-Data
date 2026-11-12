"""semantic_config 的 SQLAlchemy Core 表定义（异构存储：SQLite / Hologres）。

产品 ER 以 ``dev_docs/spec/01-语义配置(开源版)-SPEC.md`` 为准。
工程表（table_column / verified_query / explore_*）保留，不进 SPEC 01 Excel 契约。
"""

from __future__ import annotations

import os

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Float,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable

# semantic_config 与 profiling 共用同一 MetaData（同库、同一次 create_all 语义）。
metadata = MetaData()


def _target_is_hologres() -> bool:
    """True 仅当 QWENPAW_DATA_DB_URL 指向 Hologres（host 带 ``hologres.aliyuncs.com``）。

    ``orientation`` 行存参数是 Hologres 特有；标准 PostgreSQL / PolarDB for PG 不认，
    DDL 会报 ``unrecognized parameter "orientation"``。据 host 自动判定只对 Hologres 注入。
    """
    return "hologres.aliyuncs.com" in os.environ.get("QWENPAW_DATA_DB_URL", "")


# --- Hologres 行存：CREATE TABLE 末尾注入 WITH (orientation='row') --------------
# 仅对 postgresql 方言 + 目标为 Hologres 时生效；SQLite / 真 PG 走默认编译。标记位放在 Table.info。
@compiles(CreateTable, "postgresql")
def _pg_create_table_with_orientation(element, compiler, **kw):  # noqa: ANN001
    sql = compiler.visit_create_table(element, **kw)
    orientation = element.element.info.get("holo_orientation")
    if orientation and _target_is_hologres():
        sql = sql.rstrip() + f" WITH (orientation='{orientation}')\n\n"
    return sql


_HOLO_ROW = {"holo_orientation": "row"}


def _pk() -> Column:
    """自增主键：SQLite→INTEGER PRIMARY KEY AUTOINCREMENT；Hologres→bigserial。"""
    return Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


def _audit() -> tuple[Column, ...]:
    """多租户隔离 + 审计 + 软删列（所有业务表通用）。"""
    return (
        Column("tenant_id", Text),
        Column("workspace_id", Text),
        Column("created_by", Text),
        Column("updated_by", Text),
        Column("is_deleted", Integer, nullable=False, server_default=text("0")),
        Column("created_at", Text),
        Column("updated_at", Text),
    )


def _sqlite_index(
    name: str, *cols, unique: bool = False, where: str | None = None
) -> Index:
    """仅在 SQLite 生成的索引（Hologres 不支持任意 CREATE INDEX / UNIQUE）。"""
    kwargs: dict = {"unique": unique}
    if where is not None:
        kwargs["sqlite_where"] = text(where)
    idx = Index(name, *cols, **kwargs)
    idx.ddl_if(dialect="sqlite")
    return idx


# ============================ 表定义（SPEC 01 正式对象 + 工程表） ============================

# P1 数据源（config 凭证列为工程列，规范只约束 Excel 面；description 为 v2.1 新增）
datasource = Table(
    "datasource",
    metadata,
    _pk(),
    Column("datasource_id", Text),
    Column("datasource_name", Text),
    Column("datasource_type", Text),
    Column("config", Text),
    Column("description", Text),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L1 业务域（SPEC 01：归属数据源）
# import_id：导入源外部编码（如 Excel domain sheet 的 domain_id）。不写 biz_domain.id，
# 仅作跨工作簿子对象关联的稳定引用键；旧协议按 domain_name 引用时为空。
biz_domain = Table(
    "biz_domain",
    metadata,
    _pk(),
    Column("datasource_id", Text),  # → datasource.datasource_id
    Column("domain_name", Text, nullable=False),
    Column("description", Text),
    Column("import_id", Text),  # 导入源外部编码；子对象跨表关联用
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L1 业务域（新版）：不与 datasource 直连。域相关的数据源范围由已晋升数据集自动派生：
# domain -> dataset（定义在某 domain 的某 datasource 上）-> datasource。
# datasource_scope 存派生出的数据源范围（$$$ 分隔），是聚合缓存列而非外键。
# 唯一性：同租户同工作空间下同名 domain 仅一个（见 uq_domain_scope_name）。
domain = Table(
    "domain",
    metadata,
    _pk(),
    Column("domain_name", Text, nullable=False),
    Column("description", Text),
    Column("datasource_scope", Text),  # $$$ 多值；由已晋升数据集自动派生的数据源范围
    Column("import_id", Text),  # 导入源外部编码；子对象跨表关联用（沿用 biz_domain）
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L2 数据集（SPEC 01：source_datasource 存数据源编码，与「数据来源」source 区分）
dataset_meta = Table(
    "dataset_meta",
    metadata,
    _pk(),
    Column("source_datasource", Text),  # → datasource.datasource_id
    Column("domain_id", Integer),
    Column("dataset_name", Text, nullable=False),
    Column("description", Text),
    Column("source", Text, nullable=False),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L2 数据集（新版）：datasource_id → datasource.datasource_id，domain_id → domain.id，其余同 dataset_meta。
# 唯一性：同一 datasource 下（同 datasource_id）同名数据集仅一个（见 uq_dataset_scope_ds_name）。
dataset = Table(
    "dataset",
    metadata,
    _pk(),
    Column("datasource_id", Text),  # → datasource.datasource_id
    Column("domain_id", Integer),  # → domain.id
    Column("dataset_name", Text, nullable=False),
    Column("description", Text),
    Column("source", Text, nullable=False),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# P2 原始表（物理表全量登记，一行一物理表；datasource_id → datasource.datasource_id）
physical_table = Table(
    "physical_table",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),  # → datasource.datasource_id
    Column(
        "table_name", Text, nullable=False
    ),  # 物理全名（含 schema，如 public.orders）
    Column("table_comment", Text),  # DDL 原始注释，原样读取，机器不改写
    Column("partition_keys", Text),  # $$$ 多值；分区键（DESC TABLE 元数据）
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# P3 原始列（datasource_id → datasource.datasource_id；table_id → physical_table.id）。
# 不存 table_name：表名经 join physical_table 获取，避免物理表改名后全局刷列数据。
table_column = Table(
    "table_column",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),  # → datasource.datasource_id
    Column("table_id", Integer, nullable=False),  # → physical_table.id
    Column("column_name", Text, nullable=False),
    Column("data_type", Text, nullable=False),  # 物理类型（DDL）
    Column("is_primary", Text, server_default=text("'N'")),  # 'Y'/'N'
    Column("is_nullable", Text, server_default=text("'Y'")),  # 'Y'/'N'
    Column("column_comment", Text),  # DDL 原始注释，原样读取
    Column("sample_values", Text),  # $$$ 多值；高频取值采样
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 物理连接（表 join；datasource_id → datasource.datasource_id）。
# 左右表用 physical_table.id 指向、左右列用 table_column.id（$$$ 多值）指向，
# 避免物理表/列改名后全局刷数据。join_type 为 enum（inner / left_outer / ...）。
physical_join = Table(
    "physical_join",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),  # → datasource.datasource_id
    Column("left_table_id", Integer, nullable=False),  # → physical_table.id
    Column("left_column_ids", Text, nullable=False),  # $$$ 多值 → table_column.id
    Column("right_table_id", Integer, nullable=False),  # → physical_table.id
    Column("right_column_ids", Text, nullable=False),  # $$$ 多值 → table_column.id
    Column(
        "join_type", Text, server_default=text("'inner'")
    ),  # inner / left_outer / ...
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L3 数据集列（SPEC 01 表名 dataset_column_meta；物理表名沿用 dataset_field 以免拆探查链路）
dataset_column_meta = Table(
    "dataset_column_meta",
    metadata,
    _pk(),
    Column("dataset_id", Integer, nullable=False),
    Column("datasource_id", Text),
    Column("domain_id", Integer),
    Column("field_name", Text, nullable=False),
    Column("description", Text),
    Column("data_type", Text),
    Column("is_primary", Text, server_default=text("'N'")),
    Column("is_nullable", Text, server_default=text("'Y'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)
dataset_field = dataset_column_meta  # 旧名别名

# L4 维度（SPEC 01：挂 datasource_id；child_dimensions 为 $$$ 分隔；priority=HIGH/MEDIUM/LOW）
dimension = Table(
    "dimension",
    metadata,
    _pk(),
    Column("datasource_id", Text),
    Column("domain_id", Integer),
    Column("dimension_name", Text, nullable=False),
    Column("description", Text),
    Column("synonyms", Text, server_default=text("''")),
    Column("dimension_type", Text),
    Column("child_dimensions", Text),  # $$$ 多值
    Column("priority", Text, server_default=text("'MEDIUM'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L4 维度字典（新版）：定义在业务域上（不挂 datasource_id），domain_id → domain.id。
# 唯一性：同一业务域下同名维度字典仅一个（见 uq_dimension_dict_scope_name）。
dimension_dict = Table(
    "dimension_dict",
    metadata,
    _pk(),
    Column("domain_id", Integer),  # → domain.id
    Column("dimension_name", Text, nullable=False),
    Column("description", Text),
    Column("synonyms", Text, server_default=text("''")),
    Column("dimension_type", Text),
    Column("child_dimensions", Text),  # $$$ 多值
    Column("priority", Text, server_default=text("'MEDIUM'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L5 维度口径（SPEC 01：expr + value_example；冗余 datasource_id/domain_id）
dataset_dimension = Table(
    "dataset_dimension",
    metadata,
    _pk(),
    Column("dataset_id", Integer, nullable=False),
    Column("dimension_id", Integer),
    Column("datasource_id", Text),
    Column("domain_id", Integer),
    Column("expr", Text, nullable=False),
    Column("value_example", Text),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L6 指标（SPEC 01：挂 datasource_id；priority=HIGH/MEDIUM/LOW）
metric_lib = Table(
    "metric_lib",
    metadata,
    _pk(),
    Column("datasource_id", Text),
    Column("domain_id", Integer),
    Column("metric_name", Text, nullable=False),
    Column("description", Text),
    Column("synonyms", Text),
    Column("priority", Text, server_default=text("'MEDIUM'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 指标字典（新版）：不再挂 datasource_id，指标定义在业务域上，domain_id → domain.id。
# 唯一性：同一业务域下同名指标仅一个（见 uq_metric_dict_scope_name）。
metric_dict = Table(
    "metric_dict",
    metadata,
    _pk(),
    Column("domain_id", Integer),  # → domain.id
    Column("metric_name", Text, nullable=False),
    Column("description", Text),
    Column("synonyms", Text),
    Column("priority", Text, server_default=text("'MEDIUM'")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# L7 指标口径（SPEC 01 表名 metric_formula_lib）
metric_formula_lib = Table(
    "metric_formula_lib",
    metadata,
    _pk(),
    Column("dataset_id", Integer),
    Column("metric_id", Integer),
    Column("datasource_id", Text),
    Column("domain_id", Integer),
    Column("expr", Text, nullable=False),
    Column("time_window", Text, nullable=False),
    Column("additivity", Text),
    Column("is_default", Integer),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)
metric_caliber = metric_formula_lib  # 旧名别名

# L8 黄金查询（已验证问题→金标 SQL；domain_id → biz_domain.id）
verified_query = Table(
    "verified_query",
    metadata,
    _pk(),
    Column("domain_id", Integer, nullable=False),  # → biz_domain.id
    Column("question", Text, nullable=False),
    Column("verified_sql", Text, nullable=False),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

weave_task = Table(
    "weave_task",
    metadata,
    _pk(),
    Column("task_id", Text),
    Column("task_name", Text),
    Column("datasource_id", Text),
    Column("weave_mode", Text, server_default=text("'FULL'")),
    Column("status", Text),
    Column("export_payload", Text),
    Column("error_msg", Text),
    Column("heartbeat_at", Float),
    Column("executor_pod", Text),
    Column("cancel_requested", Integer, nullable=False, server_default=text("0")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# ============================ mg-088 物理层自动探查（工程表） ============================
# 命名锁：本卡一律 explore_scope（spec §1.4 三方歧义表）——与 mgw6-scope 读侧
# DatasourceScope（context_manager/api/datasource_scope.py）及身份 DatasourceScope
# （datasource_active_api.py）严格区分，不得出现新的 DatasourceScope 用法。
# 两张表均为工程表，不进 12-sheet 工作簿契约（工作簿=语义建模交换格式）。

# 表 1：explore_scope —— per-datasource 探查范围配置（一行一 ds）。
# schemas/include/exclude 为 $$$ 多值；exclude_patterns 支持保留 token
# @date_suffix（日期后缀分片规则 _\d{6,8}$，LIKE 无法表达正则，spec §1.3-2）。
explore_scope = Table(
    "explore_scope",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("schemas", Text),  # schema 白名单；空 = connector 默认
    Column("include_patterns", Text),  # 表名包含规则（SQL LIKE）；空 = 不限
    Column("exclude_patterns", Text),  # 排除规则；自动默认 = tmp% + @date_suffix
    Column("sample_mode", Text, server_default=text("'auto'")),  # on / off / auto
    Column("sample_limit", Integer, server_default=text("10")),  # 每列 distinct 上限
    Column("table_limit", Integer, server_default=text("300")),  # 单次探查表数上限
    Column("enabled", Integer, server_default=text("1")),
    Column(
        "origin", Text, server_default=text("'auto'")
    ),  # auto=首织自动 / manual=用户改过
    # 运行汇总（v1 不单独建 run 表）：
    Column("last_run_at", Text),
    Column("last_status", Text),  # running / ok / partial / failed
    Column(
        "last_counts", Text
    ),  # JSON 文本 {tables_new, tables_refreshed, columns, ...}
    Column("last_error", Text),
    Column("heartbeat_at", Float),
    Column("executor_pod", Text),
    Column("run_epoch", Integer, nullable=False, server_default=text("0")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 表 2：explore_state —— per-table 探查状态与结构指纹（一行一表）。
# fingerprint = sha1 结构指纹（列名/类型/主键/可空；comment 与 samples 不入指纹，
# 裁决 Q6）；存编辑器层不存图（图契约零新增，spec §2.3/§4.6）。
explore_state = Table(
    "explore_state",
    metadata,
    _pk(),
    Column("datasource_id", Text, nullable=False),
    Column("table_name", Text, nullable=False),  # 两段全名（与 Table.name 同形态）
    Column("fingerprint", Text),
    Column("run_id", Text),  # EXPLORE_<ts>_<rand>
    Column("status", Text),  # ok / failed / skipped_manual
    Column("column_count", Integer),
    Column("sampled", Integer, server_default=text("0")),
    *_audit(),
    info=_HOLO_ROW,
    sqlite_autoincrement=True,
)

# 用户在工作空间内选择的当前数据源（复合主键，无自增/软删；CM 同步路径也读写此表）。
active_datasource_selection = Table(
    "active_datasource_selection",
    metadata,
    Column("tenant_id", Text, nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("datasource_id", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    PrimaryKeyConstraint("tenant_id", "workspace_id", "user_id"),
    info=_HOLO_ROW,
)


# ============================ 索引（仅 SQLite） ============================
# 常用查询索引
_sqlite_index("idx_dataset_domain", dataset_meta.c.domain_id)
_sqlite_index("idx_physical_table_ds", physical_table.c.datasource_id)
_sqlite_index(
    "idx_table_column_ds_table", table_column.c.datasource_id, table_column.c.table_id
)
_sqlite_index("idx_table_column_table", table_column.c.table_id)
_sqlite_index("idx_physical_join_ds", physical_join.c.datasource_id)
_sqlite_index("idx_physical_join_left", physical_join.c.left_table_id)
_sqlite_index("idx_physical_join_right", physical_join.c.right_table_id)
_sqlite_index("idx_dataset_field_dataset", dataset_field.c.dataset_id)
_sqlite_index("idx_dimension_domain", dimension.c.domain_id)
_sqlite_index("idx_dimension_dict_domain", dimension_dict.c.domain_id)
_sqlite_index("idx_dsdim_dataset", dataset_dimension.c.dataset_id)
_sqlite_index("idx_dsdim_dimension", dataset_dimension.c.dimension_id)
_sqlite_index("idx_metric_domain", metric_lib.c.domain_id)
_sqlite_index("idx_metric_dict_domain", metric_dict.c.domain_id)
_sqlite_index("idx_caliber_metric", metric_caliber.c.metric_id)
_sqlite_index("idx_caliber_dataset", metric_caliber.c.dataset_id)
_sqlite_index("idx_verified_query_domain", verified_query.c.domain_id)
_sqlite_index("idx_weave_datasource", weave_task.c.datasource_id)
_sqlite_index("idx_explore_scope_ds", explore_scope.c.datasource_id)
_sqlite_index("idx_explore_state_ds", explore_state.c.datasource_id)

# 多租户行级隔离索引（tenant_id + workspace_id）
_sqlite_index(
    "idx_datasource_tenant_ws", datasource.c.tenant_id, datasource.c.workspace_id
)
_sqlite_index(
    "idx_biz_domain_tenant_ws", biz_domain.c.tenant_id, biz_domain.c.workspace_id
)
_sqlite_index(
    "idx_biz_domain_import",
    biz_domain.c.tenant_id,
    biz_domain.c.workspace_id,
    biz_domain.c.import_id,
)
_sqlite_index("idx_domain_tenant_ws", domain.c.tenant_id, domain.c.workspace_id)
_sqlite_index(
    "idx_domain_import",
    domain.c.tenant_id,
    domain.c.workspace_id,
    domain.c.import_id,
)
_sqlite_index(
    "idx_dataset_meta_tenant_ws", dataset_meta.c.tenant_id, dataset_meta.c.workspace_id
)
_sqlite_index("idx_dataset_tenant_ws", dataset.c.tenant_id, dataset.c.workspace_id)
_sqlite_index("idx_dataset_ds", dataset.c.datasource_id)
_sqlite_index(
    "idx_physical_table_tenant_ws",
    physical_table.c.tenant_id,
    physical_table.c.workspace_id,
)
_sqlite_index(
    "idx_table_column_tenant_ws",
    table_column.c.tenant_id,
    table_column.c.workspace_id,
)
_sqlite_index(
    "idx_physical_join_tenant_ws",
    physical_join.c.tenant_id,
    physical_join.c.workspace_id,
)
_sqlite_index(
    "idx_dataset_field_tenant_ws",
    dataset_field.c.tenant_id,
    dataset_field.c.workspace_id,
)
_sqlite_index(
    "idx_dimension_tenant_ws", dimension.c.tenant_id, dimension.c.workspace_id
)
_sqlite_index(
    "idx_dimension_dict_tenant_ws",
    dimension_dict.c.tenant_id,
    dimension_dict.c.workspace_id,
)
_sqlite_index(
    "idx_dataset_dimension_tenant_ws",
    dataset_dimension.c.tenant_id,
    dataset_dimension.c.workspace_id,
)
_sqlite_index(
    "idx_metric_lib_tenant_ws", metric_lib.c.tenant_id, metric_lib.c.workspace_id
)
_sqlite_index(
    "idx_metric_dict_tenant_ws", metric_dict.c.tenant_id, metric_dict.c.workspace_id
)
_sqlite_index(
    "idx_metric_caliber_tenant_ws",
    metric_caliber.c.tenant_id,
    metric_caliber.c.workspace_id,
)
_sqlite_index(
    "idx_verified_query_tenant_ws",
    verified_query.c.tenant_id,
    verified_query.c.workspace_id,
)
_sqlite_index(
    "idx_weave_task_tenant_ws", weave_task.c.tenant_id, weave_task.c.workspace_id
)
_sqlite_index(
    "idx_explore_scope_tenant_ws",
    explore_scope.c.tenant_id,
    explore_scope.c.workspace_id,
)
_sqlite_index(
    "idx_explore_state_tenant_ws",
    explore_state.c.tenant_id,
    explore_state.c.workspace_id,
)

# 活跃记录的 scoped 部分唯一索引（... WHERE is_deleted = 0）
_sqlite_index(
    "uq_datasource_scope_code",
    datasource.c.tenant_id,
    datasource.c.workspace_id,
    datasource.c.datasource_id,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_biz_domain_scope_name",
    biz_domain.c.tenant_id,
    biz_domain.c.workspace_id,
    biz_domain.c.datasource_id,
    biz_domain.c.domain_name,
    unique=True,
    where="is_deleted = 0",
)
# 同租户同工作空间下同名 domain 仅一个（domain 不与 datasource 直连，故不含 datasource 维度）
_sqlite_index(
    "uq_domain_scope_name",
    domain.c.tenant_id,
    domain.c.workspace_id,
    domain.c.domain_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_dataset_scope_name",
    dataset_meta.c.tenant_id,
    dataset_meta.c.workspace_id,
    dataset_meta.c.domain_id,
    dataset_meta.c.dataset_name,
    unique=True,
    where="is_deleted = 0",
)
# 同一 datasource 下（同 datasource_id）同名数据集仅一个
_sqlite_index(
    "uq_dataset_scope_ds_name",
    dataset.c.tenant_id,
    dataset.c.workspace_id,
    dataset.c.datasource_id,
    dataset.c.dataset_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_physical_table_scope_name",
    physical_table.c.tenant_id,
    physical_table.c.workspace_id,
    physical_table.c.datasource_id,
    physical_table.c.table_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_table_column_scope_key",
    table_column.c.tenant_id,
    table_column.c.workspace_id,
    table_column.c.table_id,
    table_column.c.column_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_physical_join_scope_key",
    physical_join.c.tenant_id,
    physical_join.c.workspace_id,
    physical_join.c.datasource_id,
    physical_join.c.left_table_id,
    physical_join.c.left_column_ids,
    physical_join.c.right_table_id,
    physical_join.c.right_column_ids,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_dataset_field_scope_name",
    dataset_field.c.tenant_id,
    dataset_field.c.workspace_id,
    dataset_field.c.dataset_id,
    dataset_field.c.field_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_dimension_scope_name",
    dimension.c.tenant_id,
    dimension.c.workspace_id,
    dimension.c.domain_id,
    dimension.c.dimension_name,
    unique=True,
    where="is_deleted = 0",
)
# 同一业务域下同名维度字典仅一个
_sqlite_index(
    "uq_dimension_dict_scope_name",
    dimension_dict.c.tenant_id,
    dimension_dict.c.workspace_id,
    dimension_dict.c.domain_id,
    dimension_dict.c.dimension_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_metric_scope_name",
    metric_lib.c.tenant_id,
    metric_lib.c.workspace_id,
    metric_lib.c.domain_id,
    metric_lib.c.metric_name,
    unique=True,
    where="is_deleted = 0",
)
# 同一业务域下同名指标仅一个
_sqlite_index(
    "uq_metric_dict_scope_name",
    metric_dict.c.tenant_id,
    metric_dict.c.workspace_id,
    metric_dict.c.domain_id,
    metric_dict.c.metric_name,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_caliber_scope_key",
    metric_caliber.c.tenant_id,
    metric_caliber.c.workspace_id,
    metric_caliber.c.metric_id,
    metric_caliber.c.dataset_id,
    metric_caliber.c.time_window,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_verified_query_scope_question",
    verified_query.c.tenant_id,
    verified_query.c.workspace_id,
    verified_query.c.domain_id,
    verified_query.c.question,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_weave_task_scope_id",
    weave_task.c.tenant_id,
    weave_task.c.workspace_id,
    weave_task.c.task_id,
    unique=True,
    where="is_deleted = 0",
)
# mg-088：explore_scope 一行一 ds；explore_state 一行一表（两段全名）
_sqlite_index(
    "uq_explore_scope_ds",
    explore_scope.c.tenant_id,
    explore_scope.c.workspace_id,
    explore_scope.c.datasource_id,
    unique=True,
    where="is_deleted = 0",
)
_sqlite_index(
    "uq_explore_state_tbl",
    explore_state.c.tenant_id,
    explore_state.c.workspace_id,
    explore_state.c.datasource_id,
    explore_state.c.table_name,
    unique=True,
    where="is_deleted = 0",
)


# 本模块建表子集（不含 profiling 表；profiling.tables 复用同一 metadata）。
# 12 实体重构：domain/dataset/dimension_dict/metric_dict 为正式表，读写全部走这四张。
# 旧表 biz_domain/dataset_meta/dimension/metric_lib 仍建表，仅供存量迁移脚本读取，
# 迁移完成后从本清单移除（SPEC 00 §7.1）。
SC_TABLE_LIST = [
    datasource,
    domain,
    dataset,
    dimension_dict,
    metric_dict,
    biz_domain,
    dataset_meta,
    physical_table,
    table_column,
    physical_join,
    dataset_column_meta,
    dimension,
    dataset_dimension,
    metric_lib,
    metric_formula_lib,
    verified_query,
    weave_task,
    active_datasource_selection,
    # mg-088 物理层自动探查工程表（非 12-sheet 工作簿契约）
    explore_scope,
    explore_state,
]

# 名称 → Table（供按表名动态访问的场景，如 weave_assembler._fetch_all）。
SC_TABLES = {t.name: t for t in SC_TABLE_LIST}
# 旧物理名别名，避免导入链/探查仍按 dataset_field、metric_caliber 取表。
SC_TABLES["dataset_field"] = dataset_column_meta
SC_TABLES["metric_caliber"] = metric_formula_lib
# 12 实体重构：按旧实体名取表的调用方（编织装配等）改指新表。旧表本身仍可经
# SC_TABLE_LIST 建出，但不再通过名字被业务代码取到。
SC_TABLES["biz_domain"] = domain
SC_TABLES["dataset_meta"] = dataset
SC_TABLES["dimension"] = dimension_dict
SC_TABLES["metric_lib"] = metric_dict
