"""语义 Excel 解析复杂度限制。"""

from __future__ import annotations

import zipfile

import openpyxl
import pytest

from context_manager.graph.semantic_template_io import parse_import_workbook


def test_rejects_excessive_uncompressed_archive(monkeypatch, tmp_path):
    path = tmp_path / "oversized.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * (1024 * 1024 + 1))

    monkeypatch.setenv("DATAPAW_XLSX_MAX_UNCOMPRESSED_MB", "1")
    with pytest.raises(ValueError, match="uncompressed size"):
        parse_import_workbook(path)


def test_rejects_too_many_rows(monkeypatch, tmp_path):
    path = tmp_path / "rows.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "datasource"
    sheet.append(["datasource_id"])
    sheet.append(["one"])
    sheet.append(["two"])
    workbook.save(path)

    monkeypatch.setenv("DATAPAW_XLSX_MAX_ROWS_PER_SHEET", "1")
    with pytest.raises(ValueError, match="MAX_ROWS_PER_SHEET"):
        parse_import_workbook(path)
