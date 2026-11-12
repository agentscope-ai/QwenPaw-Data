"""聚合表达式抽取。供 CALIBER / DICT —— 被聚合的列是指标候选。

移植自 ``profiling/algorithm/profiler_metric_caliber.py``
（expression_metrics_from_select / scalar_aggs_from_select /
metric_name_from_agg）+ ``query_context._extract_metric_records`` 的
per-SQL 去重与聚合。

原版关键行为（全部保留）：

- **expression 分支**：带别名且内部含 >=1 个非窗口聚合的投影整体成为一个
  复合指标（``ROUND(SUM(x), 2) AS amt`` → expression/"amt"），并**认领**其中
  的 agg 节点，交给标量分支跳过，避免同一个 ``SUM(x)`` 被数两次；
- **scalar 分支**：SELECT 投影与 HAVING 里未被认领的聚合，**逐列**独立解析
  血缘各出一行；``COUNT(*)`` 与 ``COUNT(1)`` 都归到列 ``"*"``、公式
  ``COUNT(*)``，且只在血缘能定位到表时保留（``"_"`` 丢弃）；
- 窗口聚合（``OVER(...)``）不计入指标；
- 函数名取 ``agg.sql_name()``；标量公式规范为 ``FN(col)``；
- metric_name 优先取**包含**该聚合的投影别名（小写），否则 ``fn_col``；
- per-SQL 去重 ``(指标名, 表, 公式, 来源, 类型)``，``count`` = SQL 条数；
- 聚合按 ``(metric_name, table_name, formula)``，metric_type / agg_* 取先出现值。

与原版的差异（方案 B）：表标识出口走 :func:`~.base.short_table_ref`
（原版只剥分区后缀、保留 schema 与逗号多表引用的原文段）。
expression 分支无法归属时 ``table_name="_"``，忠实原版**保留**该行，
由下游任务自行过滤。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlglot import exp

from ..snapshots.query_log import REQUIRED_EVIDENCE
from .base import rows_to_frame, short_table_ref
from .sql_analysis import SqlAnalysis


def is_window_agg(agg: exp.AggFunc) -> bool:
    """判断聚合函数是否位于 OVER(...) 窗口内，窗口聚合不计入指标。"""
    parent: exp.Expression | None = agg.parent
    while parent is not None:
        if isinstance(parent, exp.Window):
            return True
        parent = parent.parent
    return False


def non_window_aggs(expr: exp.Expression) -> list[exp.AggFunc]:
    return [agg for agg in expr.find_all(exp.AggFunc) if not is_window_agg(agg)]


def resolve_metric_table(
    analysis: SqlAnalysis,
    select: exp.Select,
    expression: exp.Expression,
) -> str:
    """推断指标归属物理表（穿透 CTE / 子查询）。"""
    table_names = analysis.resolve_expression_source_table_names(select, expression)
    return analysis.convert_table_names_to_profile_table_ref(table_names)


def agg_dependencies(expr: exp.Expression) -> tuple[str, str]:
    """从表达式中收集聚合函数名与列名（逗号拼接，保序去重）。"""
    fns: list[str] = []
    cols: list[str] = []
    seen_fn: set[str] = set()
    seen_col: set[str] = set()

    for agg in non_window_aggs(expr):
        fn = str(agg.sql_name()).upper()
        if fn not in seen_fn:
            seen_fn.add(fn)
            fns.append(fn)
        for col_expr in agg.find_all(exp.Column):
            col = SqlAnalysis.extract_column_name(col_expr)
            if col and col not in seen_col:
                seen_col.add(col)
                cols.append(col)

    return ",".join(fns), ",".join(cols)


def expression_metrics_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
) -> tuple[list[dict[str, Any]], set[int]]:
    """从 SELECT 投影中提取复合指标（含别名且表达式内包含聚合函数）。

    返回 ``(rows, claimed_agg_ids)``；后者交给
    :func:`scalar_aggs_from_select` 跳过，避免同一个聚合被数两次。
    """
    rows: list[dict[str, Any]] = []
    claimed_agg_ids: set[int] = set()

    for proj in select.expressions:
        if not isinstance(proj, exp.Alias):
            continue

        alias_name = str(proj.alias or "").strip().lower()
        if not alias_name:
            continue

        expr = proj.this
        aggs = non_window_aggs(expr)
        if not aggs:
            continue

        for agg in aggs:
            claimed_agg_ids.add(id(agg))

        phys = resolve_metric_table(analysis, select, expr)
        agg_fn, agg_col = agg_dependencies(expr)
        rows.append(
            {
                "metric_name": alias_name,
                "table_name": phys,
                "source": "select_expr",
                "formula": expr.sql(dialect=analysis.dialect_name),
                "metric_type": "expression",
                "agg_function": agg_fn,
                "agg_column": agg_col,
            }
        )

    return rows, claimed_agg_ids


def scalar_aggs_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
    *,
    skip_agg_ids: set[int] | None = None,
) -> list[tuple[str, str, str, exp.AggFunc, str]]:
    """从 SELECT 和 HAVING 中提取标量聚合，元素为 ``(fn, 表, 列, agg节点, 来源)``。"""
    out: list[tuple[str, str, str, exp.AggFunc, str]] = []
    seen: set[tuple[str, str, str]] = set()
    lineage = analysis.resolve_select_source_table_names(select)
    single_table = next(iter(lineage)) if len(lineage) == 1 else None
    lineage_table = analysis.convert_table_names_to_profile_table_ref(lineage)

    roots: list[tuple[str, exp.Expression]] = [("select", select)]
    having = select.args.get("having")
    if having is not None:
        roots.append(("having", having))

    for source, root in roots:
        if source == "select":
            search_roots = list(select.expressions)
        else:
            search_roots = [root]

        for search_root in search_roots:
            for agg in search_root.find_all(exp.AggFunc):
                if is_window_agg(agg):
                    continue
                if skip_agg_ids and id(agg) in skip_agg_ids:
                    continue

                fn = str(agg.sql_name()).upper()
                cols = list(agg.find_all(exp.Column))

                is_count_without_columns = not cols and fn == "COUNT" and bool(lineage)
                if isinstance(agg.this, exp.Star) or is_count_without_columns:
                    phys = single_table or lineage_table
                    if phys == "_":
                        continue
                    key = (fn, phys, "*")
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((fn, phys, "*", agg, source))
                    continue

                for col_expr in cols:
                    col = analysis.extract_column_name(col_expr)
                    if not col:
                        continue
                    table_names = analysis.resolve_expression_source_table_names(
                        select, col_expr
                    )
                    phys = analysis.convert_table_names_to_profile_table_ref(
                        table_names
                    )
                    if phys == "_":
                        continue
                    key = (fn, phys, col)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((fn, phys, col, agg, source))

    return out


def metric_name_from_agg(
    sel: exp.Select,
    agg: exp.AggFunc,
    fn: str,
    col: str,
) -> tuple[str, str]:
    """优先使用 SELECT 中的 AS 别名，否则按 ``fn_col`` 规则生成 metric_name。"""
    formula = f"{fn}(*)" if col == "*" else f"{fn}({col})"

    for proj in sel.expressions:
        target = proj
        alias: str | None = None
        if isinstance(proj, exp.Alias):
            alias = str(proj.alias or "").strip().lower()
            target = proj.this
        if target == agg or agg in target.find_all(exp.AggFunc):
            if alias:
                return alias, formula

    if col == "*":
        return f"{fn.lower()}_star", formula
    return f"{fn.lower()}_{col}", formula


class MetricExprParser:
    """按 ``(指标名, 表, 公式)`` 聚合，两步有序：复合表达式先认领 agg 节点。"""

    kind = "metric_expr"

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        # 归一在 result() 出口做，方言从 visit 记下来（同 SqlTemplateParser）
        self._dialect = SqlAnalysis.DEFAULT_DIALECT

    def visit(self, analysis: SqlAnalysis) -> None:
        self._dialect = analysis.dialect_name
        seen: set[tuple[str, str, str, str, str]] = set()
        for select in analysis.select_exprs:
            expression_rows, claimed_ids = expression_metrics_from_select(
                analysis, select
            )
            for row in expression_rows:
                key = (
                    row["metric_name"],
                    row["table_name"],
                    row["formula"],
                    row["source"],
                    row["metric_type"],
                )
                if key not in seen:
                    seen.add(key)
                    self._rows.append(row)
            for fn, table, column, aggregate, source in scalar_aggs_from_select(
                analysis, select, skip_agg_ids=claimed_ids
            ):
                metric_name, formula = metric_name_from_agg(
                    select, aggregate, fn, column
                )
                key = (metric_name, table, formula, source, "scalar_agg")
                if key in seen:
                    continue
                seen.add(key)
                self._rows.append(
                    {
                        "metric_name": metric_name,
                        "table_name": table,
                        "source": source,
                        "formula": formula,
                        "metric_type": "scalar_agg",
                        "agg_function": fn,
                        "agg_column": column,
                    }
                )

    def result(self) -> pd.DataFrame:
        columns = REQUIRED_EVIDENCE["metric_expr"]
        if not self._rows:
            return rows_to_frame([], columns)
        frame = pd.DataFrame(self._rows)
        frame["table_name"] = frame["table_name"].map(
            lambda ref: short_table_ref(ref, self._dialect)
        )
        return (
            frame.groupby(["metric_name", "table_name", "formula"], as_index=False)
            .agg(
                count=("metric_name", "size"),
                metric_type=("metric_type", "first"),
                sources=("source", lambda values: list(values.unique())),
                agg_function=("agg_function", "first"),
                agg_column=("agg_column", "first"),
            )
            .sort_values(["table_name", "metric_name"])
            .reset_index(drop=True)
        )[list(columns)]


__all__ = [
    "MetricExprParser",
    "agg_dependencies",
    "expression_metrics_from_select",
    "is_window_agg",
    "metric_name_from_agg",
    "non_window_aggs",
    "resolve_metric_table",
    "scalar_aggs_from_select",
]
