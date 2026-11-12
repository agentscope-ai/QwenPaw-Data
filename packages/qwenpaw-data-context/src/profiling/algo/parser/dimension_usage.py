"""维度用法抽取。供 CALIBER / DICT —— 被 GROUP BY / 等值过滤的列是维度候选。

移植自 ``profiling/algorithm/profiler_dimension_caliber.py``
（group_dimensions_from_select / where_dimensions_from_select）
+ ``query_context._extract_dimension_records`` 的 per-SQL 去重与聚合。

原版关键行为（全部保留）：

- GROUP BY 对每个表达式 ``find_all(Column)``，``date_trunc(col)`` 这类包裹
  也能取到列；``GROUP BY 1, 2`` 序号引用不含 Column 节点，自然不产出；
- WHERE 只拆顶层 AND，只认 ``col = 字面量``（列在左）与 ``col IN (...)``，
  ``col > 100`` / ``LIKE`` 等非等值谓词不算维度使用；
- 列必须**跨作用域唯一**归属物理表（穿透 CTE / 派生表），否则整行丢弃；
- per-SQL 去重 ``(表, 列, 来源)``，``count`` = 出现该用法的 SQL 条数；
- 列名统一小写（``extract_column_name``）。

与原版的差异：出口列名 ``dimension_name`` → ``column_name``（snapshot 契约），
表标识走 :func:`~.base.short_table_ref`（方案 B；原版只剥分区后缀）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlglot import exp

from ..snapshots.query_log import REQUIRED_EVIDENCE
from .base import rows_to_frame, short_table_ref
from .sql_analysis import SqlAnalysis


def is_literal_like(node: exp.Expression | None) -> bool:
    """WHERE 中 col = ? 的右侧是否为字面量。"""
    if node is None:
        return False
    if isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        return True
    if isinstance(node, exp.Cast) and is_literal_like(node.this):
        return True
    return False


def _append_dimension(
    rows: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    table_name: str | None,
    col: str,
    source: str,
) -> None:
    if not col or col == "*" or table_name is None:
        return
    key = (table_name, col, source)
    if key in seen:
        return
    seen.add(key)
    rows.append({"dimension_name": col, "table_name": table_name, "source": source})


def group_dimensions_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
) -> list[dict[str, Any]]:
    """从一个 SELECT 的 GROUP BY 提取维度列。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    group = select.args.get("group")
    if not group:
        return rows

    for group_expr in group.expressions:
        for col_expr in group_expr.find_all(exp.Column):
            _append_dimension(
                rows,
                seen,
                analysis.resolve_unique_column_source_table_name(select, col_expr),
                analysis.extract_column_name(col_expr),
                "group_by",
            )
    return rows


def where_dimensions_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
) -> list[dict[str, Any]]:
    """从一个 SELECT 的 WHERE 提取维度列（仅 ``col = 字面量`` 与 ``col IN (...)``）。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    where = select.args.get("where")
    if where is None:
        return rows

    cond = where.this if isinstance(where, exp.Where) else where
    for part in analysis.extract_conjunct_exprs(cond):
        col_expr: exp.Column | None = None
        if isinstance(part, exp.EQ):
            left, right = part.left, part.right
            if isinstance(left, exp.Column) and is_literal_like(right):
                col_expr = left
        elif isinstance(part, exp.In) and isinstance(part.this, exp.Column):
            col_expr = part.this

        if col_expr is None:
            continue
        _append_dimension(
            rows,
            seen,
            analysis.resolve_unique_column_source_table_name(select, col_expr),
            analysis.extract_column_name(col_expr),
            "where",
        )
    return rows


class DimensionUsageParser:
    """按 ``(表, 列)`` 聚合维度用法，记录来源子句与 SQL 条数。"""

    kind = "dimension_usage"

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        # 归一在 result() 出口做，方言从 visit 记下来（同 SqlTemplateParser）
        self._dialect = SqlAnalysis.DEFAULT_DIALECT

    def visit(self, analysis: SqlAnalysis) -> None:
        self._dialect = analysis.dialect_name
        seen: set[tuple[str, str, str]] = set()
        for select in analysis.select_exprs:
            extracted = group_dimensions_from_select(
                analysis, select
            ) + where_dimensions_from_select(analysis, select)
            for row in extracted:
                key = (row["table_name"], row["dimension_name"], row["source"])
                if key not in seen:
                    seen.add(key)
                    self._rows.append(row)

    def result(self) -> pd.DataFrame:
        columns = REQUIRED_EVIDENCE["dimension_usage"]
        if not self._rows:
            return rows_to_frame([], columns)
        frame = pd.DataFrame(self._rows)
        frame["table_name"] = frame["table_name"].map(
            lambda ref: short_table_ref(ref, self._dialect)
        )
        return (
            frame.groupby(["dimension_name", "table_name"], as_index=False)
            .agg(
                count=("dimension_name", "size"),
                sources=("source", lambda values: list(values.unique())),
            )
            .rename(columns={"dimension_name": "column_name"})
            .sort_values(["table_name", "column_name"])
            .reset_index(drop=True)
        )[list(columns)]


__all__ = [
    "DimensionUsageParser",
    "group_dimensions_from_select",
    "is_literal_like",
    "where_dimensions_from_select",
]
