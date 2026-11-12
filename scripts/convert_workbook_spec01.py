"""把旧 12-sheet 语义工作簿转换成 SPEC 01 的 7-sheet 形态（一次性工具）。"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parents[1] / "packages" / "qwenpaw-data-context" / "src"
sys.path.insert(0, str(_PKG_SRC))

from openpyxl import Workbook, load_workbook  # noqa: E402

from semantic_config import template_contract as tc  # noqa: E402

SEP = tc.MULTI_VALUE_SEPARATOR


def _txt(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def main(src: str, dst: str) -> None:
    wb = load_workbook(src, read_only=True, data_only=True)

    def rows(name: str) -> list[dict]:
        if name not in wb.sheetnames:
            return []
        it = wb[name].iter_rows(values_only=True)
        header = [(_txt(h) or "") for h in next(it)]
        out = []
        for i, raw in enumerate(it, start=2):
            if raw is None or all(c is None for c in raw):
                continue
            r = {
                header[j]: (raw[j] if j < len(raw) else None)
                for j in range(len(header))
            }
            r["_row"] = i
            out.append(r)
        return out

    src_domain = rows("biz_domain")
    src_dataset = rows("dataset")
    src_column = rows("dataset_column")
    src_dim = rows("dimension")
    src_binding = rows("dataset_dimension")
    src_metric = rows("metric")
    src_formula = rows("metric_formula")

    # SPEC 01：口径上的 dimension_type 退出契约，类型改挂维度字典 → 按维度名回填。
    dim_type: dict[str, str] = {}
    for b in src_binding:
        name, t = _txt(b.get("dimension_name")), _txt(b.get("dimension_type"))
        if name and t and name not in dim_type:
            dim_type[name] = t

    # parent_name 已退役，反推成父维度上的 child_dimensions（顺序即优先顺序）。
    children: dict[str, list[str]] = defaultdict(list)
    for d in src_dim:
        child, parent = _txt(d.get("dimension_name")), _txt(d.get("parent_name"))
        if child and parent:
            children[parent].append(child)

    out: dict[str, list[list]] = {}

    out["biz_domain"] = [
        [_txt(r.get("domain_name")), _txt(r.get("description"))] for r in src_domain
    ]
    out["dataset"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("dataset_name")),
            _txt(r.get("dataset_comment")),
            _txt(r.get("sql_content")),
        ]
        for r in src_dataset
    ]
    out["dataset_column"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("dataset_name")),
            _txt(r.get("column_name")),
            _txt(r.get("column_comment")),
            _txt(r.get("data_type")),
            _txt(r.get("is_primary")),
            _txt(r.get("is_nullable")),
        ]
        for r in src_column
    ]
    out["dimension"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("dimension_name")),
            _txt(r.get("description")),
            _txt(r.get("synonyms")),
            dim_type.get(_txt(r.get("dimension_name")) or ""),
            SEP.join(children.get(_txt(r.get("dimension_name")) or "", [])) or None,
            None,  # priority：旧表无来源，留空由导入侧落 MEDIUM
        ]
        for r in src_dim
    ]
    out["dataset_dimension"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("dimension_name")),
            _txt(r.get("dataset_name")),
            _txt(r.get("calculate_expr")),
            None,  # value_example：旧表无样例值列
        ]
        for r in src_binding
    ]
    out["metric"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("metric_name")),
            _txt(r.get("description")),
            _txt(r.get("synonyms")),
            # D4 既有约定：is_polaris=1 → priority=HIGH
            "HIGH" if _txt(r.get("is_polaris")) == "1" else "MEDIUM",
        ]
        for r in src_metric
    ]
    out["metric_formula"] = [
        [
            _txt(r.get("domain_name")),
            _txt(r.get("metric_name")),
            _txt(r.get("dataset_name")),
            _txt(r.get("formula")),
            _txt(r.get("date_range")),
            None,
            None,  # additivity / is_default 旧表无来源
        ]
        for r in src_formula
    ]

    new = Workbook()
    new.remove(new.active)
    for sheet in tc.IMPORT_ORDER:
        ws = new.create_sheet(sheet)
        ws.append(tc.SHEET_SPECS[sheet].header())
        for row in out[sheet]:
            ws.append(row)
    meta = new.create_sheet("_meta")
    meta.append(["key", "value"])
    meta.append(["template_version", tc.TEMPLATE_VERSION])
    meta.append(["converted_from", Path(src).name])
    new.save(dst)

    print(f"已生成: {dst}")
    for sheet in tc.IMPORT_ORDER:
        print(f"  {sheet:18} {len(out[sheet])} 行")

    print("\n=== 必填列为空、导入会报错的行（需人工补或删行）===")
    any_bad = False
    for sheet in tc.IMPORT_ORDER:
        spec = tc.SHEET_SPECS[sheet]
        required_idx = [(i, c.name) for i, c in enumerate(spec.columns) if c.required]
        for n, row in enumerate(out[sheet], start=2):
            missing = [name for i, name in required_idx if row[i] is None]
            if missing:
                any_bad = True
                label = " / ".join(str(v) for v in row[:3] if v)
                print(f"  {sheet} 第{n}行 缺 {missing}  ({label})")
    if not any_bad:
        print("  无")

    # time_window 参与自然键：同 (metric, dataset, time_window) 会后写覆盖
    seen: dict[tuple, int] = {}
    dup = []
    for n, row in enumerate(out["metric_formula"], start=2):
        key = (row[1], row[2], row[4])
        if key in seen:
            dup.append((seen[key], n, key))
        seen[key] = n
    print("\n=== metric_formula 自然键重复（后写覆盖前写）===")
    print("  无" if not dup else "")
    for first, second, key in dup:
        print(f"  第{first}行 与 第{second}行 同键 {key}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
