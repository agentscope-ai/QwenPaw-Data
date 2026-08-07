-- SQLite 建表 DDL（由 Hologres 终态库表平迁而来）
-- 约定：
--   主键     id INTEGER PRIMARY KEY AUTOINCREMENT（单调不复用；取回用 RETURNING id）
--   布尔     INTEGER 0/1（应用层转 bool）
--   时间     TEXT，ISO8601 带偏移（应用层写入）
--   软删除   is_deleted INTEGER DEFAULT 0，查询带 WHERE is_deleted = 0
--   关联     全部 BIGINT id（此处 INTEGER），无 DB 外键约束，一致性由应用层维护

-- 1. 数据源
CREATE TABLE IF NOT EXISTS datasource (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id   TEXT,
    datasource_name TEXT,
    datasource_type TEXT, -- odps pg 。。
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    config          TEXT
);

-- 2. 业务域
CREATE TABLE IF NOT EXISTS biz_domain (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_name   TEXT,
    display_name  TEXT,
    description   TEXT,
    aliases       TEXT,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
);

-- 3. 数据集
CREATE TABLE IF NOT EXISTS dataset_meta (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id   TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id       INTEGER,
    dataset_name    TEXT NOT NULL,
    dataset_comment TEXT,
    dataset_type    TEXT,
    sql_content     TEXT,
    parents         TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);

-- 4. 列
CREATE TABLE IF NOT EXISTS dataset_column_meta (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id               INTEGER NOT NULL,
    datasource_id            TEXT,     -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id                INTEGER,
    column_name              TEXT NOT NULL,
    is_primary               TEXT DEFAULT 'N',
    is_nullable              TEXT DEFAULT 'Y',
    data_type                TEXT,
    column_comment           TEXT,
    column_enums             TEXT DEFAULT '',
    column_enums_description TEXT DEFAULT '',
    column_type              TEXT DEFAULT '',
    samples                  TEXT DEFAULT '',
    dimension_type           TEXT DEFAULT '',
    column_name_cn           TEXT,
    is_deleted               INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT,
    updated_at               TEXT
);

-- 5. 维度主表
CREATE TABLE IF NOT EXISTS dimension (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id  TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id      INTEGER,
    dimension_name TEXT NOT NULL,
    description    TEXT,
    parent_name    TEXT,
    depth          INTEGER,
    synonyms       TEXT DEFAULT '',
    is_visible     INTEGER DEFAULT 1,
    is_attribution INTEGER DEFAULT 1,
    enums          TEXT,
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT
);

-- 6. 维度口径
CREATE TABLE IF NOT EXISTS dataset_dimension (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id     INTEGER NOT NULL,
    dimension_id   INTEGER,
    datasource_id  TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id      INTEGER,
    calculate_expr TEXT,
    dimension_type TEXT,
    data_type      TEXT,
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT
);

-- 7. 维度值
CREATE TABLE IF NOT EXISTS dataset_dimension_value (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id          INTEGER NOT NULL,
    dimension_id        INTEGER,
    datasource_id       TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id           INTEGER,
    dimension_value     TEXT,
    dimension_occur_cnt INTEGER DEFAULT 0,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT,
    updated_at          TEXT
);

-- 8. 指标
CREATE TABLE IF NOT EXISTS metric_lib (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id     TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id         INTEGER,
    metric_name       TEXT NOT NULL,
    description       TEXT,
    unit              TEXT,
    is_polaris        INTEGER DEFAULT 0,
    show_distribution INTEGER DEFAULT 0,
    is_visible        INTEGER DEFAULT 1,
    synonyms          TEXT,
    tags              TEXT DEFAULT '[]',
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

-- 9. 指标口径
CREATE TABLE IF NOT EXISTS metric_formula_lib (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id        INTEGER,
    dataset_id       INTEGER,
    datasource_id    TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    domain_id        INTEGER,
    formula          TEXT,
    date_range       TEXT,
    formula_evidence TEXT,
    derived_from     TEXT,
    evidence_ext     TEXT,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    updated_at       TEXT
);

-- 10. 语义编织任务
CREATE TABLE IF NOT EXISTS weave_task (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT,
    task_name      TEXT,
    datasource_id  TEXT,          -- 数据源对外编码, 关联 datasource.datasource_id
    weave_mode     TEXT DEFAULT 'FULL',
    status         TEXT,
    export_payload TEXT,
    error_msg      TEXT,
    created_by     TEXT,
    updated_by     TEXT,
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT
);

-- 常用查询索引（非唯一；唯一性/同名校验在应用层做）
CREATE INDEX IF NOT EXISTS idx_biz_domain_ds        ON biz_domain(datasource_id);
CREATE INDEX IF NOT EXISTS idx_dataset_domain       ON dataset_meta(domain_id);
CREATE INDEX IF NOT EXISTS idx_column_dataset       ON dataset_column_meta(dataset_id);
CREATE INDEX IF NOT EXISTS idx_dimension_domain     ON dimension(domain_id);
CREATE INDEX IF NOT EXISTS idx_dsdim_dataset        ON dataset_dimension(dataset_id);
CREATE INDEX IF NOT EXISTS idx_dsdim_dimension      ON dataset_dimension(dimension_id);
CREATE INDEX IF NOT EXISTS idx_dsdimval_dataset     ON dataset_dimension_value(dataset_id);
CREATE INDEX IF NOT EXISTS idx_metric_domain        ON metric_lib(domain_id);
CREATE INDEX IF NOT EXISTS idx_formula_metric       ON metric_formula_lib(metric_id);
CREATE INDEX IF NOT EXISTS idx_formula_dataset      ON metric_formula_lib(dataset_id);
CREATE INDEX IF NOT EXISTS idx_weave_datasource     ON weave_task(datasource_id);
