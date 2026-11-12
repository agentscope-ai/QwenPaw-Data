"""SPEC 01 开源版 Excel 模板契约（7 张产品 sheet）。

权威源
------
``dev_docs/spec/01-语义配置(开源版)-SPEC.md`` §6.9 / §7.1：

- 无 ``datasource`` sheet，表内不出现 ``datasource_id`` / ``source_datasource``；
  归属数据源由导入请求参数 ``datasource_id`` 绑定。
- 逻辑层 sheet：biz_domain、dataset、dataset_column、dimension、dataset_dimension、
  metric、metric_formula、verified_query；物理层 sheet：table、table_column、
  physical_join；均可选；无任何已知 sheet → 400。
- 父对象一律按名称引用（``domain_name`` / ``dataset_name`` / ``dimension_name`` /
  ``metric_name`` / ``table_name``），verified_query 也用 ``domain_name``
  （SPEC 00 §12.1 统一域引用列）；业务域是工作空间级对象、按名在工作空间内解析，
  字典在域内解析，数据集/口径在请求 ``datasource_id`` 范围内解析（SPEC 00 §12.2）。
  旧模板的 ``domain_id``（= domain.import_id）导入侧仍兼容读入，但不再是契约列。
- 多值列以 ``$$$`` 拼接。

sheet 名归一（旧/新命名互认）在导入服务层完成，不在本契约维护别名表；本模块只持有
SPEC 01 正名的列契约（旧 12-sheet 别名兼容已移除）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: 多值列分隔符（SPEC §3.3）。
MULTI_VALUE_SEPARATOR = "$$$"

#: 模板版本号（_meta.template_version）。
TEMPLATE_VERSION = "spec-01"

#: 横切审计列：SPEC 01 产品 Excel 不输出这些列（保留常量供旧调用方引用）。
SOURCE_CHANNEL_COLUMN = "source_channel"
UPDATED_AT_COLUMN = "updated_at"
CROSS_CUTTING_LOGICAL: tuple[str, ...] = ()
CROSS_CUTTING_PHYSICAL: tuple[str, ...] = ()

PRODUCTION_AUTO = "自动"
PRODUCTION_CANDIDATE = "候选"
PRODUCTION_MANUAL = "人工"
PRODUCTION_AUTO_OR_CANDIDATE = "自动/候选"

_BOOL_NOTE = (
    "模板面布尔 1/0；存储面 is_primary/is_nullable 为 'Y'/'N'，is_default 为 1/0"
)


@dataclass(frozen=True)
class ColumnSpec:
    """模板面一列的契约定义（列名/必填/多值 $$$/布尔/key/产出/备注）。"""

    name: str
    required: bool = False
    multi: bool = False
    boolean: bool = False
    key: bool = False
    production: str = PRODUCTION_MANUAL
    note: str = ""


@dataclass(frozen=True)
class SheetSpec:
    """模板面一个 sheet 的契约定义。"""

    name: str
    title: str
    layer: str  # SPEC 01 产品 sheet 均为 logical
    storage_table: str | None
    columns: tuple[ColumnSpec, ...]
    cross_cutting_notes: tuple[str, ...] = ()

    @property
    def readonly(self) -> bool:
        return self.layer == "physical"

    @property
    def cross_cutting(self) -> tuple[str, ...]:
        return ()

    def header(self) -> list[str]:
        return [c.name for c in self.columns]

    def template_header(self) -> list[str]:
        return self.header()

    def column(self, name: str) -> ColumnSpec:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"sheet {self.name!r} 无列 {name!r}")


def _c(
    name: str,
    required: bool = False,
    *,
    multi: bool = False,
    boolean: bool = False,
    key: bool = False,
    production: str = PRODUCTION_MANUAL,
    note: str = "",
) -> ColumnSpec:
    return ColumnSpec(
        name=name,
        required=required,
        multi=multi,
        boolean=boolean,
        key=key,
        production=production,
        note=note,
    )


_BIZ_DOMAIN = SheetSpec(
    name="biz_domain",
    title="业务域",
    layer="logical",
    storage_table="domain",
    columns=(
        _c(
            "domain_name",
            True,
            key=True,
            note="业务域名称；工作空间内唯一（已与数据源解耦）",
        ),
        _c("description", True, note="业务域描述"),
    ),
)

_DATASET = SheetSpec(
    name="dataset",
    title="数据集",
    layer="logical",
    storage_table="dataset",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("dataset_name", True, key=True, note="数据集名称；同一数据源内唯一"),
        _c("description", note="数据集描述"),
        _c(
            "source", True, production=PRODUCTION_AUTO_OR_CANDIDATE, note="数据来源 SQL"
        ),
    ),
)

_DATASET_COLUMN = SheetSpec(
    name="dataset_column",
    title="数据集列",
    layer="logical",
    storage_table="dataset_column_meta",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("dataset_name", True, key=True, note="按名称引用数据集"),
        _c("field_name", True, key=True, note="字段名称；同一数据集内唯一"),
        _c("description", True, note="字段描述"),
        _c("data_type", note="数据类型"),
        _c("is_primary", boolean=True, note=_BOOL_NOTE),
        _c("is_nullable", boolean=True, note=_BOOL_NOTE),
    ),
)

_DIMENSION = SheetSpec(
    name="dimension",
    title="维度字典",
    layer="logical",
    storage_table="dimension_dict",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("dimension_name", True, key=True, note="维度名称；同一业务域内唯一"),
        _c("description", note="维度描述"),
        _c("synonyms", multi=True, note="同义词，$$$ 分隔"),
        _c("dimension_type", note="维度类型，见附录 A"),
        _c("child_dimensions", multi=True, note="子维度名称，$$$ 分隔"),
        _c("priority", note="HIGH / MEDIUM / LOW，缺省 MEDIUM"),
    ),
)

_DATASET_DIMENSION = SheetSpec(
    name="dataset_dimension",
    title="维度口径",
    layer="logical",
    storage_table="dataset_dimension",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("dimension_name", True, key=True, note="按名称引用维度"),
        _c("dataset_name", True, key=True, note="按名称引用数据集"),
        _c("expr", True, note="口径表达式"),
        _c("value_example", multi=True, note="样例值，$$$ 分隔"),
    ),
)

_METRIC = SheetSpec(
    name="metric",
    title="指标字典",
    layer="logical",
    storage_table="metric_dict",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("metric_name", True, key=True, note="度量名；同一业务域内唯一"),
        _c("description", note="度量描述"),
        _c("synonyms", multi=True, note="同义词，$$$ 分隔"),
        _c("priority", note="HIGH / MEDIUM / LOW，缺省 MEDIUM"),
    ),
)

_METRIC_FORMULA = SheetSpec(
    name="metric_formula",
    title="指标口径",
    layer="logical",
    storage_table="metric_formula_lib",
    columns=(
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("metric_name", True, key=True, note="按名称引用指标"),
        _c("dataset_name", True, key=True, note="按名称引用数据集"),
        _c("expr", True, note="口径表达式"),
        _c(
            "time_window",
            key=True,
            note="时间窗口，参与自然键；可空（空单元格视为空串）",
        ),
        _c("additivity", note="ADDITIVE / SEMI_ADDITIVE / NON_ADDITIVE"),
        _c("is_default", boolean=True, note=_BOOL_NOTE),
    ),
)

# ---- 物理层 sheet（table / table_column / physical_join）：归属数据源由请求参数绑定 ----
# 表/列在文件内均无 id，靠 table_name / column_name 名称引用；导入侧解析为 id 落库。
_TABLE = SheetSpec(
    name="table",
    title="原始表",
    layer="physical",
    storage_table="physical_table",
    columns=(
        _c(
            "table_name",
            True,
            key=True,
            note="物理表全名（含 schema，如 public.orders）",
        ),
        _c("table_comment", note="DDL 原始注释，原样读取"),
        _c("partition_keys", multi=True, note="分区键，$$$ 分隔"),
    ),
)

_TABLE_COLUMN = SheetSpec(
    name="table_column",
    title="原始列",
    layer="physical",
    storage_table="table_column",
    columns=(
        _c("table_name", True, key=True, note="按表名引用原始表（table sheet）"),
        _c("column_name", True, key=True, note="列名；同一表内唯一"),
        _c("data_type", True, note="物理类型（DDL）"),
        _c("is_primary", boolean=True, note=_BOOL_NOTE),
        _c("is_nullable", boolean=True, note=_BOOL_NOTE),
        _c("column_comment", note="DDL 原始注释，原样读取"),
        _c("sample_values", multi=True, note="样例值，$$$ 分隔"),
    ),
)

_PHYSICAL_JOIN = SheetSpec(
    name="physical_join",
    title="物理连接",
    layer="physical",
    storage_table="physical_join",
    columns=(
        _c("left_table", True, key=True, note="按表名引用左表"),
        _c("left_columns", True, multi=True, key=True, note="左连接列名，$$$ 分隔"),
        _c("right_table", True, key=True, note="按表名引用右表"),
        _c("right_columns", True, multi=True, key=True, note="右连接列名，$$$ 分隔"),
        _c("join_type", note="inner / left_outer 等，缺省 inner"),
    ),
)

_VERIFIED_QUERY = SheetSpec(
    name="verified_query",
    title="黄金查询",
    layer="logical",
    storage_table="verified_query",
    columns=(
        # SPEC 00 §12.1：域引用全表统一为 domain_name。旧模板的 domain_id
        # （= domain.import_id）导入侧仍兼容读入，但不再是契约列。
        _c("domain_name", True, key=True, note="按名称引用业务域"),
        _c("question", True, key=True, note="自然语言问题"),
        _c("verified_sql", True, production=PRODUCTION_CANDIDATE, note="金标 SQL"),
    ),
)

IMPORT_ORDER: tuple[str, ...] = (
    "biz_domain",
    "table",
    "table_column",
    "physical_join",
    "dataset",
    "dataset_column",
    "dimension",
    "dataset_dimension",
    "metric",
    "metric_formula",
    "verified_query",
)

SHEET_SPECS: dict[str, SheetSpec] = {
    s.name: s
    for s in (
        _BIZ_DOMAIN,
        _TABLE,
        _TABLE_COLUMN,
        _PHYSICAL_JOIN,
        _DATASET,
        _DATASET_COLUMN,
        _DIMENSION,
        _DATASET_DIMENSION,
        _METRIC,
        _METRIC_FORMULA,
        _VERIFIED_QUERY,
    )
}
assert tuple(SHEET_SPECS) == IMPORT_ORDER

_META_SHEET = "_meta"

PRODUCT_OBJECTS: tuple[str, ...] = IMPORT_ORDER + ("all",)


def iter_sheets() -> Iterator[SheetSpec]:
    for name in IMPORT_ORDER:
        yield SHEET_SPECS[name]


def sheet_names(include_meta: bool = False) -> list[str]:
    names = list(IMPORT_ORDER)
    if include_meta:
        names.append(_META_SHEET)
    return names


def canonicalize_sheet_name(name: str) -> str | None:
    """把工作簿 sheet 名规范成 SPEC 01 名；未知则返回 None。

    已移除旧 12-sheet 别名兼容：仅受理 SPEC 01 正名，旧工作簿先用
    ``scripts/convert_workbook_spec01.py`` 一次性转换。
    """
    if name in SHEET_SPECS:
        return name
    return None


def _read_workbook_headers(path: str | Path) -> tuple[list[str], dict[str, list[str]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True)
    try:
        headers: dict[str, list[str]] = {}
        for name in wb.sheetnames:
            row = next(wb[name].iter_rows(min_row=1, max_row=1, values_only=True), ())
            header = list(row)
            while header and header[-1] is None:
                header.pop()
            headers[name] = [str(v) for v in header]
        return list(wb.sheetnames), headers
    finally:
        wb.close()


def compare_instance_workbook(path: str | Path) -> list[str]:
    """比对工作簿 header 与本契约（只核 SPEC 01 7 张产品 sheet）。"""
    mismatches: list[str] = []
    _sheetnames, headers = _read_workbook_headers(path)
    for name in IMPORT_ORDER:
        if name not in headers:
            mismatches.append(f"缺 sheet：{name}")
            continue
        expected = SHEET_SPECS[name].header()
        got = headers[name]
        if got != expected:
            mismatches.append(f"{name}：header {got} != 契约业务列 {expected}")
    return mismatches


__all__ = [
    "MULTI_VALUE_SEPARATOR",
    "TEMPLATE_VERSION",
    "SOURCE_CHANNEL_COLUMN",
    "UPDATED_AT_COLUMN",
    "CROSS_CUTTING_LOGICAL",
    "CROSS_CUTTING_PHYSICAL",
    "PRODUCTION_AUTO",
    "PRODUCTION_CANDIDATE",
    "PRODUCTION_MANUAL",
    "PRODUCTION_AUTO_OR_CANDIDATE",
    "ColumnSpec",
    "SheetSpec",
    "IMPORT_ORDER",
    "SHEET_SPECS",
    "PRODUCT_OBJECTS",
    "iter_sheets",
    "sheet_names",
    "canonicalize_sheet_name",
    "compare_instance_workbook",
]
