"""Excel 模板契约测试：逻辑层 8 sheet + 物理层 3 sheet，无 datasource 列。"""

from __future__ import annotations

import openpyxl
import pytest

from semantic_config import template_contract as tc
from semantic_config.template_generator import (
    build_blank_workbook,
    generate_blank_template,
)

EXPECTED_IMPORT_ORDER = [
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
]

#: 物理层 sheet（layer="physical"）。
PHYSICAL_SHEETS = {"table", "table_column", "physical_join"}

EXPECTED_HEADERS = {
    "biz_domain": ["domain_name", "description"],
    "table": ["table_name", "table_comment", "partition_keys"],
    "table_column": [
        "table_name",
        "column_name",
        "data_type",
        "is_primary",
        "is_nullable",
        "column_comment",
        "sample_values",
    ],
    "physical_join": [
        "left_table",
        "left_columns",
        "right_table",
        "right_columns",
        "join_type",
    ],
    "dataset": ["domain_name", "dataset_name", "description", "source"],
    "dataset_column": [
        "domain_name",
        "dataset_name",
        "field_name",
        "description",
        "data_type",
        "is_primary",
        "is_nullable",
    ],
    "dimension": [
        "domain_name",
        "dimension_name",
        "description",
        "synonyms",
        "dimension_type",
        "child_dimensions",
        "priority",
    ],
    "dataset_dimension": [
        "domain_name",
        "dimension_name",
        "dataset_name",
        "expr",
        "value_example",
    ],
    "metric": ["domain_name", "metric_name", "description", "synonyms", "priority"],
    "metric_formula": [
        "domain_name",
        "metric_name",
        "dataset_name",
        "expr",
        "time_window",
        "additivity",
        "is_default",
    ],
    # SPEC 00 §12.1：域引用全表统一 domain_name（原 domain_id 按 import_id 的列作废）。
    "verified_query": ["domain_name", "question", "verified_sql"],
}

EXPECTED_REQUIRED = {
    "biz_domain": {"domain_name", "description"},
    "table": {"table_name"},
    "table_column": {"table_name", "column_name", "data_type"},
    "physical_join": {"left_table", "left_columns", "right_table", "right_columns"},
    "dataset": {"domain_name", "dataset_name", "source"},
    "dataset_column": {"domain_name", "dataset_name", "field_name", "description"},
    "dimension": {"domain_name", "dimension_name"},
    "dataset_dimension": {"domain_name", "dimension_name", "dataset_name", "expr"},
    "metric": {"domain_name", "metric_name"},
    "metric_formula": {
        "domain_name",
        "metric_name",
        "dataset_name",
        "expr",
    },
    "verified_query": {"domain_name", "question", "verified_sql"},
}

ALL_SHEETS = list(EXPECTED_HEADERS)


def test_import_order_matches_contract():
    assert list(tc.IMPORT_ORDER) == EXPECTED_IMPORT_ORDER
    assert len(tc.IMPORT_ORDER) == 11
    assert "datasource" not in tc.IMPORT_ORDER


def test_sheet_specs_cover_import_order_exactly():
    assert set(tc.SHEET_SPECS) == set(EXPECTED_IMPORT_ORDER)
    assert list(tc.SHEET_SPECS) == EXPECTED_IMPORT_ORDER


@pytest.mark.parametrize("sheet", ALL_SHEETS)
def test_sheet_header_matches_spec(sheet):
    assert tc.SHEET_SPECS[sheet].header() == EXPECTED_HEADERS[sheet]
    assert "datasource_id" not in tc.SHEET_SPECS[sheet].header()
    assert "source_datasource" not in tc.SHEET_SPECS[sheet].header()


@pytest.mark.parametrize("sheet", ALL_SHEETS)
def test_required_columns_match_spec(sheet):
    got = {c.name for c in tc.SHEET_SPECS[sheet].columns if c.required}
    assert got == EXPECTED_REQUIRED[sheet]


def test_multi_value_separator_and_strict_sheet_names():
    assert tc.MULTI_VALUE_SEPARATOR == "$$$"
    # 旧 12-sheet 别名兼容已移除：旧名一律不受理，仅 SPEC 01 正名。
    assert tc.canonicalize_sheet_name("dataset_field") is None
    assert tc.canonicalize_sheet_name("domain") is None
    assert tc.canonicalize_sheet_name("biz_domain") == "biz_domain"
    assert tc.canonicalize_sheet_name("datasource") is None


def test_no_cross_cutting_columns_on_product_sheets():
    for name in ALL_SHEETS:
        spec = tc.SHEET_SPECS[name]
        assert spec.cross_cutting == ()
        assert spec.template_header() == spec.header()
        assert spec.readonly is (name in PHYSICAL_SHEETS)


def test_build_blank_workbook_structure():
    wb = build_blank_workbook(datasource_code="demo")
    assert wb.sheetnames == EXPECTED_IMPORT_ORDER + ["_meta"]
    for sheet in EXPECTED_IMPORT_ORDER:
        header = [c.value for c in wb[sheet][1]]
        assert header == tc.SHEET_SPECS[sheet].template_header()
        assert wb[sheet].max_row == 1


def test_build_blank_workbook_single_object():
    wb = build_blank_workbook(object_name="biz_domain")
    assert wb.sheetnames == ["biz_domain", "_meta"]


def test_build_blank_workbook_meta_rows():
    wb = build_blank_workbook(datasource_code="demo", domain_id="chatapp")
    ws = wb["_meta"]
    assert [c.value for c in ws[1]] == ["key", "value"]
    meta = {r[0]: r[1] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert meta["template_version"].startswith("spec-01")
    assert meta["datasource_code"] == "demo"


def test_generate_blank_template_writes_file(tmp_path):
    out = tmp_path / "blank-template-spec-01.xlsx"
    result = generate_blank_template(out, datasource_code="demo")
    assert result == out
    assert out.exists()
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == EXPECTED_IMPORT_ORDER + ["_meta"]


def test_generate_blank_template_refuses_overwrite(tmp_path):
    out = tmp_path / "t.xlsx"
    generate_blank_template(out, datasource_code="x")
    with pytest.raises(FileExistsError):
        generate_blank_template(out, datasource_code="x")
