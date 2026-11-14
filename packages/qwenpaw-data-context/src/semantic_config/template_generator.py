"""SPEC 01 空白模板生成。

生成的空白模板与 ``template_contract`` 契约一致：7 张产品 sheet（顺序 =
IMPORT_ORDER）+ ``_meta``；无 datasource sheet，无数据源编码列。

CLI::

    uv run python -m semantic_config.template_generator \
        --out /tmp/sc-blank-template-spec-01.xlsx --datasource-code postgresql-demo-gaap
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from semantic_config.template_contract import (
    IMPORT_ORDER,
    SHEET_SPECS,
    TEMPLATE_VERSION,
    sheet_names,
)

#: 规范文档链接（_meta.spec_doc）；开源版留空，契约以 ``template_contract`` 为准。
SPEC_DOC_URL = ""

#: _meta.template_version 的完整写法（与实例工作簿一致）。
TEMPLATE_VERSION_FULL = f"{TEMPLATE_VERSION}（sheets：{'/'.join(IMPORT_ORDER)}）"

_BOLD = Font(bold=True)


def build_blank_workbook(
    datasource_code: str = "",
    domain_id: str = "",
    object_name: str | None = None,
) -> Workbook:
    """构造空白模板工作簿（内存态，未落盘）。

    - 7 张产品 sheet（或 ``object_name`` 指定的单对象 sheet）：仅表头行；
    - ``_meta``：key/value 两列 + template_version/spec_doc/created_at/
      datasource_code/domain_id。
    """
    sheets = list(IMPORT_ORDER)
    if object_name and object_name not in ("all", ""):
        if object_name not in SHEET_SPECS:
            raise ValueError(f"未知对象: object={object_name}")
        sheets = [object_name]
    wb = Workbook()
    wb.remove(wb.active)
    for name in sheets:
        spec = SHEET_SPECS[name]
        ws = wb.create_sheet(title=name)
        ws.append(spec.template_header())
        for cell in ws[1]:
            cell.font = _BOLD
    ws_meta = wb.create_sheet(title="_meta")
    ws_meta.append(["key", "value"])
    for cell in ws_meta[1]:
        cell.font = _BOLD
    for key, value in (
        ("template_version", TEMPLATE_VERSION_FULL),
        ("spec_doc", SPEC_DOC_URL),
        ("created_at", datetime.now().isoformat(timespec="seconds")),
        ("datasource_code", datasource_code),
        ("domain_id", domain_id),
    ):
        ws_meta.append([key, value])
    return wb


def generate_blank_template(
    out_path: str | Path,
    datasource_code: str = "",
    domain_id: str = "",
    overwrite: bool = False,
    object_name: str | None = None,
) -> Path:
    """把空白模板写到 ``out_path`` 并返回路径；目标已存在时抛 FileExistsError。"""
    out = Path(out_path)
    if out.exists() and not overwrite:
        raise FileExistsError(f"目标已存在：{out}（传 overwrite=True 覆盖）")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = build_blank_workbook(
        datasource_code=datasource_code,
        domain_id=domain_id,
        object_name=object_name,
    )
    wb.save(out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 SPEC 01 的 7-sheet 空白 Excel 模板（含 _meta）。"
    )
    parser.add_argument("--out", required=True, help="输出 xlsx 路径")
    parser.add_argument(
        "--datasource-code", default="", help="_meta.datasource_code（如 postgresql-demo-gaap）"
    )
    parser.add_argument(
        "--domain-id", default="", help="_meta.domain_id（如 DemoProduct）"
    )
    parser.add_argument("--overwrite", action="store_true", help="目标已存在时覆盖")
    args = parser.parse_args(argv)
    try:
        out = generate_blank_template(
            args.out,
            datasource_code=args.datasource_code,
            domain_id=args.domain_id,
            overwrite=args.overwrite,
        )
    except FileExistsError as exc:
        parser.error(str(exc))
    print(
        f"已生成空白模板：{out}（sheets: {', '.join(sheet_names(include_meta=True))}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
