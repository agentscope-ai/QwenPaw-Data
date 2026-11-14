-- 语义探查（profiling）子系统建表 DDL（幂等）。
-- 约定沿用 semantic_config：
--   主键     id INTEGER PRIMARY KEY AUTOINCREMENT
--   布尔     INTEGER 0/1
--   时间     TEXT，ISO8601 带偏移
--   软删     is_deleted INTEGER DEFAULT 0
--   关联     父子用 BIGINT id；唯独数据源用其对外唯一编码 datasource_id（TEXT，如
--            postgresql-1a2b3c4e），指向 datasource.datasource_id。无 DB 外键，一致性由应用层维护。
--   多租户   单库行级隔离：tenant_id + workspace_id（从数据源派生）
--   审计     created_by / updated_by（principal.user_id；异步任务用提交时捕获值）
-- 与 semantic_config 共用同一 SQLite 库文件，启动时与 semantic_config schema 一并 init。

-- 阶段状态：每数据源 6 行（BIZ_DOMAIN/DATASET/DIMENSION_CALIBER/DIMENSION_MANAGE/METRIC_CALIBER/METRIC_MANAGE）
CREATE TABLE IF NOT EXISTS probe_stage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id   TEXT NOT NULL,               -- → datasource.datasource_id
    stage           TEXT NOT NULL,               -- 6 个阶段常量
    status          TEXT NOT NULL DEFAULT 'NOT_STARTED', -- NOT_STARTED/PROBING/REVIEWING/COMPLETED/SKIPPED
    current_task_id TEXT,                          -- 最近一次探查任务 task_id
    tenant_id       TEXT,
    workspace_id    TEXT,
    created_by      TEXT,
    updated_by      TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_probe_stage ON probe_stage(datasource_id, stage);

-- 探查任务：一次异步探查一行（可留痕历史）
CREATE TABLE IF NOT EXISTS probe_task (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,                -- PROBE_yyyyMMddHHmmss_<4位随机>
    datasource_id   TEXT NOT NULL,                -- → datasource.datasource_id
    stage           TEXT NOT NULL,                -- 6 个阶段常量（与 probe_stage 对齐）
    status          TEXT NOT NULL,                -- RUNNING/SUCCESS/STOPPED/FAILED
    progress        INTEGER DEFAULT 0,            -- 0~100
    message         TEXT,                         -- 进度文案
    error_msg       TEXT,
    tenant_id       TEXT,
    workspace_id    TEXT,
    created_by      TEXT,
    updated_by      TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    finished_at     TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_task_ds ON probe_task(datasource_id, stage);
CREATE UNIQUE INDEX IF NOT EXISTS uq_probe_task_id ON probe_task(task_id);

-- 业务域探查 staging
CREATE TABLE IF NOT EXISTS staging_biz_domain (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id TEXT NOT NULL,           -- → datasource.datasource_id
    task_id       TEXT,                    -- → probe_task.task_id
    domain_name   TEXT,
    display_name  TEXT,
    description   TEXT,
    aliases       TEXT,
    member_tables TEXT,                    -- JSON 数组，聚类进本域的物理表清单（只读佐证）
    source_type   TEXT DEFAULT 'ALGO',     -- ALGO/HUMAN
    probe_status  TEXT DEFAULT 'PENDING',  -- PENDING/CONFIRMED/SKIPPED
    tenant_id     TEXT,
    workspace_id  TEXT,
    created_by    TEXT,
    updated_by    TEXT,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
);

-- 数据集探查 staging
CREATE TABLE IF NOT EXISTS staging_dataset (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id     TEXT NOT NULL,       -- → datasource.datasource_id
    task_id           TEXT,                -- → probe_task.task_id
    domain_id         INTEGER,             -- → 正式 biz_domain.id
    domain_name       TEXT,                -- 写时冗余，读路径 SELECT * 直接返回
    dataset_name      TEXT,
    dataset_comment   TEXT,
    dataset_type      TEXT,
    sql_content       TEXT,
    parents           TEXT,
    source_type       TEXT DEFAULT 'ALGO',
    probe_status      TEXT DEFAULT 'PENDING',
    tenant_id         TEXT,
    workspace_id      TEXT,
    created_by        TEXT,
    updated_by        TEXT,
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

-- 维度口径探查 staging
CREATE TABLE IF NOT EXISTS staging_dataset_dimension (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id       TEXT NOT NULL,     -- → datasource.datasource_id
    task_id             TEXT,              -- → probe_task.task_id
    domain_id           INTEGER,
    domain_name         TEXT,              -- 写时冗余，读路径 SELECT * 直接返回
    dataset_id          INTEGER,           -- → 正式 dataset_meta.id
    dataset_name        TEXT,              -- 写时冗余，读路径 SELECT * 直接返回
    dimension_name      TEXT,
    calculate_expr      TEXT,
    dimension_type      TEXT,
    data_type           TEXT,
    source_type         TEXT DEFAULT 'ALGO',
    probe_status        TEXT DEFAULT 'PENDING',
    tenant_id           TEXT,
    workspace_id        TEXT,
    created_by          TEXT,
    updated_by          TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT,
    updated_at          TEXT
);

-- 维度实体 staging（管理确认页，由算法直接探查）
CREATE TABLE IF NOT EXISTS staging_dimension (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id     TEXT NOT NULL,       -- → datasource.datasource_id
    task_id           TEXT,                -- → probe_task.task_id
    domain_id         INTEGER,
    domain_name       TEXT,                -- 写时冗余，读路径 SELECT * 直接返回
    dimension_name    TEXT,
    description       TEXT,
    parent_name       TEXT,
    depth             INTEGER,
    synonyms          TEXT,
    is_visible        INTEGER DEFAULT 1,
    is_attribution    INTEGER DEFAULT 0,
    enums             TEXT,
    source_type       TEXT DEFAULT 'ALGO',
    probe_status      TEXT DEFAULT 'PENDING',
    tenant_id         TEXT,
    workspace_id      TEXT,
    created_by        TEXT,
    updated_by        TEXT,
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

-- 指标口径探查 staging
CREATE TABLE IF NOT EXISTS staging_metric_formula (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id     TEXT NOT NULL,       -- → datasource.datasource_id
    task_id           TEXT,                -- → probe_task.task_id
    domain_id         INTEGER,
    domain_name       TEXT,                -- 写时冗余，读路径 SELECT * 直接返回
    dataset_id        INTEGER,
    dataset_name      TEXT,                -- 写时冗余，读路径 SELECT * 直接返回
    metric_name       TEXT,
    formula           TEXT,
    date_range        TEXT,
    formula_evidence  TEXT,
    derived_from      TEXT,
    source_type       TEXT DEFAULT 'ALGO',
    probe_status      TEXT DEFAULT 'PENDING',
    tenant_id         TEXT,
    workspace_id      TEXT,
    created_by        TEXT,
    updated_by        TEXT,
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

-- 指标实体 staging（管理确认页，由算法直接探查）
CREATE TABLE IF NOT EXISTS staging_metric (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id     TEXT NOT NULL,       -- → datasource.datasource_id
    task_id           TEXT,                -- → probe_task.task_id
    domain_id         INTEGER,
    domain_name       TEXT,                -- 写时冗余，读路径 SELECT * 直接返回
    metric_name       TEXT,
    description       TEXT,
    unit              TEXT,
    is_polaris        INTEGER DEFAULT 0,
    show_distribution INTEGER DEFAULT 0,
    is_visible        INTEGER DEFAULT 1,
    synonyms          TEXT,
    tags              TEXT DEFAULT '[]',
    source_type       TEXT DEFAULT 'ALGO',
    probe_status      TEXT DEFAULT 'PENDING',
    tenant_id         TEXT,
    workspace_id      TEXT,
    created_by        TEXT,
    updated_by        TEXT,
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

-- 多租户行级隔离索引（tenant_id + workspace_id）
CREATE INDEX IF NOT EXISTS idx_probe_stage_tenant_ws              ON probe_stage(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_probe_task_tenant_ws               ON probe_task(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_biz_domain_tenant_ws       ON staging_biz_domain(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_dataset_tenant_ws          ON staging_dataset(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_dataset_dimension_tenant_ws ON staging_dataset_dimension(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_dimension_tenant_ws        ON staging_dimension(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_metric_formula_tenant_ws   ON staging_metric_formula(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_staging_metric_tenant_ws           ON staging_metric(tenant_id, workspace_id);
