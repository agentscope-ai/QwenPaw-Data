"""语义配置存储表契约：12 实体正式对象字段 + 工程表登记。

按 SPEC 00（12 实体重构）锚定：业务域与数据源解耦，正式表为
``domain`` / ``dataset`` / ``dimension_dict`` / ``metric_dict``；
旧表 ``biz_domain`` / ``dataset_meta`` / ``dimension`` / ``metric_lib`` 仍建表，
仅供存量迁移脚本读取（§7.1），迁移完成后从 SC_TABLE_LIST 摘除。

断言一律按**物理表名**取表（``BY_NAME``），不走 ``SC_TABLES``——后者带 name 别名
（``SC_TABLES["biz_domain"] is domain``），用它会把旧名解析到新表、验不出真实结构。
别名本身由 ``test_name_aliases_point_to_new_tables`` 单独盯。
"""

from __future__ import annotations

import sqlalchemy as sa
from semantic_config.tables import SC_TABLE_LIST, SC_TABLES, metadata

AUDIT = {
    "tenant_id",
    "workspace_id",
    "created_by",
    "updated_by",
    "is_deleted",
    "created_at",
    "updated_at",
}

#: 物理表名 → Table，供本模块所有结构断言使用。
BY_NAME = {t.name: t for t in SC_TABLE_LIST}


EXPECTED_COLUMNS: dict[str, set[str]] = {
    "datasource": {
        "id",
        "datasource_id",
        "datasource_name",
        "datasource_type",
        "config",
        "description",
    }
    | AUDIT,
    # 业务域：去 datasource_id，+ datasource_scope（派生缓存）；import_id 沿用。
    "domain": {
        "id",
        "domain_name",
        "description",
        "datasource_scope",
        "import_id",
    }
    | AUDIT,
    # 数据集：source_datasource 更名 datasource_id，是域↔数据源之间唯一的桥。
    "dataset": {
        "id",
        "datasource_id",
        "domain_id",
        "dataset_name",
        "description",
        "source",
    }
    | AUDIT,
    "dimension_dict": {
        "id",
        "domain_id",
        "dimension_name",
        "description",
        "synonyms",
        "dimension_type",
        "child_dimensions",
        "priority",
    }
    | AUDIT,
    "metric_dict": {
        "id",
        "domain_id",
        "metric_name",
        "description",
        "synonyms",
        "priority",
    }
    | AUDIT,
    "physical_table": {
        "id",
        "datasource_id",
        "table_name",
        "table_comment",
        "partition_keys",
    }
    | AUDIT,
    # 原始列按 table_id 挂表（不再冗余 table_name）。
    "table_column": {
        "id",
        "datasource_id",
        "table_id",
        "column_name",
        "data_type",
        "is_primary",
        "is_nullable",
        "column_comment",
        "sample_values",
    }
    | AUDIT,
    "physical_join": {
        "id",
        "datasource_id",
        "left_table_id",
        "left_column_ids",
        "right_table_id",
        "right_column_ids",
        "join_type",
    }
    | AUDIT,
    "dataset_column_meta": {
        "id",
        "dataset_id",
        "datasource_id",
        "domain_id",
        "field_name",
        "description",
        "data_type",
        "is_primary",
        "is_nullable",
    }
    | AUDIT,
    "dataset_dimension": {
        "id",
        "dataset_id",
        "dimension_id",
        "datasource_id",
        "domain_id",
        "expr",
        "value_example",
    }
    | AUDIT,
    "metric_formula_lib": {
        "id",
        "metric_id",
        "dataset_id",
        "datasource_id",
        "domain_id",
        "expr",
        "time_window",
        "additivity",
        "is_default",
    }
    | AUDIT,
    "verified_query": {
        "id",
        "domain_id",
        "question",
        "verified_sql",
    }
    | AUDIT,
    # ---- 迁移期并存的旧表（结构冻结在重构前形态，仅供迁移脚本读取）----
    "biz_domain": {
        "id",
        "datasource_id",
        "domain_name",
        "description",
        "import_id",
    }
    | AUDIT,
    "dataset_meta": {
        "id",
        "source_datasource",
        "domain_id",
        "dataset_name",
        "description",
        "source",
    }
    | AUDIT,
    "dimension": {
        "id",
        "datasource_id",
        "domain_id",
        "dimension_name",
        "description",
        "synonyms",
        "dimension_type",
        "child_dimensions",
        "priority",
    }
    | AUDIT,
    "metric_lib": {
        "id",
        "datasource_id",
        "domain_id",
        "metric_name",
        "description",
        "synonyms",
        "priority",
    }
    | AUDIT,
    # ---- 工程表 ----
    "weave_task": {
        "id",
        "task_id",
        "task_name",
        "datasource_id",
        "weave_mode",
        "status",
        "export_payload",
        "error_msg",
        "cancel_requested",
        "heartbeat_at",
        "executor_pod",
    }
    | AUDIT,
    "active_datasource_selection": {
        "tenant_id",
        "workspace_id",
        "user_id",
        "datasource_id",
        "updated_at",
    },
    "explore_scope": {
        "id",
        "datasource_id",
        "schemas",
        "include_patterns",
        "exclude_patterns",
        "sample_mode",
        "sample_limit",
        "table_limit",
        "enabled",
        "origin",
        "last_run_at",
        "last_status",
        "last_counts",
        "last_error",
        "heartbeat_at",
        "executor_pod",
        "run_epoch",
    }
    | AUDIT,
    "explore_state": {
        "id",
        "datasource_id",
        "table_name",
        "fingerprint",
        "run_id",
        "status",
        "column_count",
        "sampled",
    }
    | AUDIT,
}

EXPECTED_UNIQUE_KEYS: dict[str, set[str]] = {
    "datasource": {"tenant_id", "workspace_id", "datasource_id"},
    # 业务域是工作空间级对象：域名不再按数据源分片。
    "domain": {"tenant_id", "workspace_id", "domain_name"},
    # 数据集唯一性从"域内"改为"数据源内"。
    "dataset": {"tenant_id", "workspace_id", "datasource_id", "dataset_name"},
    "dimension_dict": {"tenant_id", "workspace_id", "domain_id", "dimension_name"},
    "metric_dict": {"tenant_id", "workspace_id", "domain_id", "metric_name"},
    "physical_table": {"tenant_id", "workspace_id", "datasource_id", "table_name"},
    "table_column": {"tenant_id", "workspace_id", "table_id", "column_name"},
    "dataset_column_meta": {"tenant_id", "workspace_id", "dataset_id", "field_name"},
    "metric_formula_lib": {
        "tenant_id",
        "workspace_id",
        "metric_id",
        "dataset_id",
        "time_window",
    },
    "verified_query": {"tenant_id", "workspace_id", "domain_id", "question"},
    "weave_task": {"tenant_id", "workspace_id", "task_id"},
    "explore_scope": {"tenant_id", "workspace_id", "datasource_id"},
    "explore_state": {"tenant_id", "workspace_id", "datasource_id", "table_name"},
}


def test_sc_table_list_matches_physical_names():
    assert {t.name for t in SC_TABLE_LIST} == set(EXPECTED_COLUMNS)
    assert set(EXPECTED_COLUMNS) <= set(SC_TABLES)


def test_every_table_matches_column_contract():
    for name, expected in EXPECTED_COLUMNS.items():
        actual = {c.name for c in BY_NAME[name].columns}
        assert actual == expected, (
            f"{name}: 多余={sorted(actual - expected)}, 缺失={sorted(expected - actual)}"
        )


def test_name_aliases_point_to_new_tables():
    """按名取表的调用方（如 Excel 导入）用旧名也必须落到新表上（SPEC 00 §7.2）。"""
    for legacy, current in (
        ("biz_domain", "domain"),
        ("dataset_meta", "dataset"),
        ("dimension", "dimension_dict"),
        ("metric_lib", "metric_dict"),
    ):
        assert SC_TABLES[legacy] is BY_NAME[current], legacy
        assert SC_TABLES[current] is BY_NAME[current], current


def test_domain_carries_derived_datasource_scope():
    """datasource_scope 是可空派生缓存列（$$$ 多值），不是业务必填字段。"""
    col = BY_NAME["domain"].c.datasource_scope
    assert col.nullable is True
    assert isinstance(col.type, sa.Text)


def test_unique_key_contract():
    for name, expected_cols in EXPECTED_UNIQUE_KEYS.items():
        t = BY_NAME[name]
        colsets = [{c.name for c in idx.columns} for idx in t.indexes if idx.unique]
        assert expected_cols in colsets, f"{name}: 唯一键={colsets}"


def test_not_null_and_defaults_contract():
    assert BY_NAME["dataset"].c.source.nullable is False
    assert BY_NAME["physical_table"].c.table_name.nullable is False
    assert BY_NAME["table_column"].c.table_id.nullable is False
    assert BY_NAME["table_column"].c.datasource_id.nullable is False
    assert BY_NAME["dataset_dimension"].c.expr.nullable is False
    assert BY_NAME["metric_formula_lib"].c.expr.nullable is False
    assert BY_NAME["metric_formula_lib"].c.time_window.nullable is False
    assert BY_NAME["metric_formula_lib"].c.is_default.nullable is True
    assert BY_NAME["domain"].c.domain_name.nullable is False
    for name in ("metric_dict", "dimension_dict"):
        default = BY_NAME[name].c.priority.server_default
        assert default is not None, f"{name}.priority 缺 server_default"
        assert "MEDIUM" in str(default.arg)


def test_create_all_on_empty_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'sc_spec01.db'}")
    metadata.create_all(engine, tables=SC_TABLE_LIST)
    names = set(sa.inspect(engine).get_table_names())
    assert set(EXPECTED_COLUMNS) <= names
    assert "dataset_dimension_value" not in names
    engine.dispose()
