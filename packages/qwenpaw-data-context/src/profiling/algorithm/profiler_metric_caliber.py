"""从历史 query logs 中提取聚合指标（SUM/COUNT/AVG 等）。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd
from sqlglot import exp

from .utils_ast import SqlAnalysis
from .data_context import SourceTableManager

logger = logging.getLogger(__name__)


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
    table_names = analysis.resolve_expression_source_table_names(
        select,
        expression,
    )
    return analysis.convert_table_names_to_profile_table_ref(table_names)


def agg_dependencies(expr: exp.Expression) -> tuple[str, str]:
    """从表达式中收集聚合函数名与列名（逗号拼接）。"""
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
    """
    从 SELECT 投影中提取复合指标（含别名且表达式内包含聚合函数）。

    例如::

        CASE WHEN SUM(a) > 0 THEN ROUND(100.0 * SUM(b) / SUM(a), 1) ELSE 0 END AS like_rate

    返回 ``(rows, claimed_agg_ids)``。``rows`` 输出的结果示例::

        [{"metric_name": "like_rate",
          "table_name": "ads_example_dashboard_overview_1d",
          "source": "select_expr",
          "formula": "CASE WHEN SUM(a) > 0 THEN ROUND(100.0 * SUM(b) / SUM(a), 1) ELSE 0 END",
          "metric_type": "expression",
          "agg_function": "SUM",
          "agg_column": "a,b"}]

    ``claimed_agg_ids`` 是已被复合指标"认领"的聚合节点 ``id()`` 集合，交给
    :func:`scalar_aggs_from_select` 跳过，避免同一个 ``SUM(a)`` 被数两次。

    Corner cases：
        - 投影没有别名、或表达式里没有非窗口聚合时不产出行；
        - 跨多表时 ``table_name`` 是逗号拼接（``"a,b"``），无法定位则为 ``"_"``。
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

        # 表归属和 SQL 方言都来自同一个分析上下文，避免调用方重复维护解析状态。
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
    """从 SELECT 和 HAVING 中提取标量聚合。

    输出的结果示例（``list[tuple]``，元素为 ``(fn, table, column, agg节点, source)``）::

        [("SUM", "ads_example_revenue_index_1d", "gmv", <AggFunc>, "select"),
         ("COUNT", "ads_example_revenue_index_1d", "*", <AggFunc>, "having")]

    Corner cases：
        - 窗口聚合与 ``skip_agg_ids`` 中的节点被跳过；
        - ``COUNT(*)`` 只在血缘能定位到表时保留，列名记作 ``"*"``；
        - 同一 ``(fn, table, column)`` 在一个 SELECT 内只保留一次。
    """
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
                        select,
                        col_expr,
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
    """优先使用 SELECT 中的 AS 别名，否则按 ``fn_col`` 规则生成 metric_name。

    输出的结果示例（``(metric_name, formula)``）::

        SUM(gmv) AS total_gmv   -> ("total_gmv", "SUM(gmv)")
        SUM(gmv)                -> ("sum_gmv",   "SUM(gmv)")
        COUNT(*)                -> ("count_star", "COUNT(*)")
    """
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


def expand_metric_column_lookup(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """
    将指标聚合结果展开为列级 lookup，便于与 tables schema join。

    输入列：``metric_name, table_name, formula, count, metric_type, sources,
    agg_function, agg_column``。

    输出的结果示例（已按 ``(table_name, column_name)`` 聚合）::

        table_name,column_name,query_log_aggregation_count,query_log_aggregation_type,formula,metric_name,is_derived_metric
        ads_example_revenue_index_1d,gmv,12,['SUM'],SUM(gmv),total_gmv,False
        ads_example_revenue_index_1d,total_gmv,12,['SUM'],SUM(gmv),total_gmv,True

    一条指标会展开成两类行：
        - 物理聚合列（``agg_column``，原表通常存在）：``is_derived_metric=False``；
        - 指标名本身（``metric_name``，原表通常不存在的"新列"）：``True``。

    Corner cases：
        - 输入为空返回列齐全的空表；
        - ``table_name`` 为 ``"a,b"`` 时按逗号拆开分别产出，``"_"`` 直接丢弃。
    """
    if metrics_df is None or metrics_df.empty:
        return pd.DataFrame(
            columns=[
                "column_name",
                "table_name",
                "query_log_aggregation_count",
                "query_log_aggregation_type",
                "formula",
                "metric_name",
                "is_derived_metric",
            ]
        )

    rows: list[dict[str, Any]] = []
    for _, row in metrics_df.iterrows():
        table_names = [
            name.strip()
            for name in str(row.get("table_name") or "").split(",")
            if name.strip() and name.strip() != "_"
        ]
        if not table_names:
            continue

        count = int(row.get("count") or 0)
        agg_type = str(row.get("agg_function") or "").strip()
        formula = str(row.get("formula") or "").strip()
        metric_name = str(row.get("metric_name") or "").strip()
        agg_cols = [
            col.strip()
            for col in str(row.get("agg_column") or "").split(",")
            if col.strip() and col.strip() != "*"
        ]

        for table_name in table_names:
            # 物理聚合列（原表中通常存在）
            for col in agg_cols:
                rows.append(
                    {
                        "table_name": table_name,
                        "column_name": col,
                        "query_log_aggregation_count": count,
                        "query_log_aggregation_type": agg_type,
                        "formula": formula,
                        "metric_name": metric_name,
                        "is_derived_metric": False,
                    }
                )
            # 派生指标名：可能是原表中不存在的“新列”
            if metric_name and metric_name not in agg_cols:
                rows.append(
                    {
                        "table_name": table_name,
                        "column_name": metric_name,
                        "query_log_aggregation_count": count,
                        "query_log_aggregation_type": agg_type or "expression",
                        "formula": formula or metric_name,
                        "metric_name": metric_name,
                        "is_derived_metric": True,
                    }
                )
            elif metric_name and not agg_cols:
                rows.append(
                    {
                        "table_name": table_name,
                        "column_name": metric_name,
                        "query_log_aggregation_count": count,
                        "query_log_aggregation_type": agg_type or "expression",
                        "formula": formula or metric_name,
                        "metric_name": metric_name,
                        "is_derived_metric": True,
                    }
                )

    if not rows:
        return pd.DataFrame(
            columns=[
                "column_name",
                "table_name",
                "query_log_aggregation_count",
                "query_log_aggregation_type",
                "formula",
                "metric_name",
                "is_derived_metric",
            ]
        )

    expanded = pd.DataFrame(rows)
    return expanded.groupby(["table_name", "column_name"], as_index=False).agg(
        query_log_aggregation_count=("query_log_aggregation_count", "sum"),
        query_log_aggregation_type=(
            "query_log_aggregation_type",
            lambda values: list(dict.fromkeys([v for v in values if v])),
        ),
        formula=("formula", "first"),
        metric_name=("metric_name", "first"),
        is_derived_metric=("is_derived_metric", "max"),
    )


def merge_metric_evidence_into_tables(
    records: pd.DataFrame | None,
    table_map: Mapping[str, pd.DataFrame] | SourceTableManager,
    *,
    allow_new_columns: bool = True,
) -> dict[str, pd.DataFrame]:
    """把日志指标证据合并进各表列 DataFrame。

    ``records`` 可以是 :meth:`QueryLogContext._extract_metric_records` 的原始输出，
    也可以是已经展开过的列级 lookup（含 ``column_name`` 与
    ``query_log_aggregation_count`` 两列时按后者处理）。

    输出 ``{table_name: 列详情 DataFrame}``，在原列详情右侧追加，示例::

        column_name,data_type,...,query_log_aggregation_count,query_log_aggregation_type,metric_name,metric_formula,is_derived_metric,is_metric_in_query_logs
        gmv,numeric,...,12.0,['SUM'],total_gmv,SUM(gmv),False,yes
        total_gmv,derived,...,12.0,['SUM'],total_gmv,SUM(gmv),True,yes
        date_id,text,...,,,,,,no

    与 dimension 的关键差异：``allow_new_columns=True`` 时，若证据中的
    ``column_name``（含派生 ``metric_name``）在原表不存在，则 **追加新行**，
    该行 ``data_type=derived``、``column_comment`` 用公式兜底。

    Corner cases：
        - ``records`` 为 ``None`` / 空表时所有列标 ``is_metric_in_query_logs=no``；
        - 重复调用幂等：先 drop 旧的证据列再重新合并。
    """
    from collections.abc import Mapping as MappingABC

    if isinstance(table_map, SourceTableManager):
        items = list(table_map.items())
    elif isinstance(table_map, MappingABC):
        items = list(table_map.items())
    else:
        items = []

    if records is None or not isinstance(records, pd.DataFrame) or records.empty:
        lookup = expand_metric_column_lookup(pd.DataFrame())
    elif {
        "column_name",
        "query_log_aggregation_count",
    }.issubset(records.columns):
        lookup = records.copy()
    else:
        lookup = expand_metric_column_lookup(records)

    for col in ("table_name", "column_name"):
        if col in lookup.columns:
            lookup[col] = lookup[col].astype(str)

    evidence_cols = [
        "column_name",
        "query_log_aggregation_count",
        "query_log_aggregation_type",
    ]
    optional_cols = ["formula", "metric_name", "is_derived_metric"]
    for c in optional_cols:
        if c in lookup.columns:
            evidence_cols.append(c)

    merged_map: dict[str, pd.DataFrame] = {}
    for table_name, table_df in items:
        if table_df is None or not isinstance(table_df, pd.DataFrame):
            logger.warning(f"Skip invalid table dataframe: {table_name}")
            continue
        current = table_df.copy() if not table_df.empty else pd.DataFrame()
        if "column_name" not in current.columns:
            current = pd.DataFrame(columns=["column_name"])
        current["column_name"] = current["column_name"].astype(str)

        drop_cols = [
            c
            for c in (
                "query_log_aggregation_count",
                "query_log_aggregation_type",
                "is_metric_in_query_logs",
                "query_log_metric_name",
                "metric_formula",
                "metric_name",
                "is_derived_metric",
                "column_role",
                "dimension_name",
                "date_range",
                "role_reason",
                "role_confidence",
                "is_dimension",
                "is_metric",
                "is_date",
            )
            if c in current.columns
        ]
        if drop_cols:
            current = current.drop(columns=drop_cols)

        if lookup.empty or "table_name" not in lookup.columns:
            table_lookup = pd.DataFrame(columns=evidence_cols)
        else:
            table_lookup = lookup[lookup["table_name"] == str(table_name)][
                evidence_cols
            ].copy()

        # 日志侧的 metric_name 是 SQL 别名，列角色阶段会把 metric_name 写成语义名；
        # 这里先改名，避免二次合并时 pandas 产生 metric_name_x / metric_name_y。
        if "metric_name" in table_lookup.columns:
            table_lookup = table_lookup.rename(
                columns={"metric_name": "query_log_metric_name"}
            )
        merged = current.merge(table_lookup, on="column_name", how="left")

        if allow_new_columns and not table_lookup.empty:
            existing = set(merged["column_name"].astype(str))
            new_rows = table_lookup[
                ~table_lookup["column_name"].astype(str).isin(existing)
            ].copy()
            if not new_rows.empty:
                # 为新列补齐原表列，避免 concat 丢列
                for col in merged.columns:
                    if col not in new_rows.columns:
                        new_rows[col] = None
                if "table_name" in merged.columns:
                    new_rows["table_name"] = str(table_name)
                elif "table_name" not in new_rows.columns:
                    new_rows["table_name"] = str(table_name)
                if "column_comment" in merged.columns:
                    # 派生指标用 formula 作为注释兜底
                    if "formula" in new_rows.columns:
                        new_rows["column_comment"] = new_rows["formula"]
                if "data_type" in merged.columns:
                    if "data_type" not in new_rows.columns:
                        new_rows["data_type"] = "derived"
                    else:
                        new_rows["data_type"] = new_rows["data_type"].fillna("derived")
                merged = pd.concat(
                    [merged, new_rows[merged.columns]], ignore_index=True
                )

        if "formula" in merged.columns and "metric_formula" not in merged.columns:
            merged = merged.rename(columns={"formula": "metric_formula"})
        elif "formula" in merged.columns and "metric_formula" in merged.columns:
            merged["metric_formula"] = merged["metric_formula"].fillna(
                merged["formula"]
            )
            merged = merged.drop(columns=["formula"])

        merged["is_metric_in_query_logs"] = (
            merged["query_log_aggregation_count"]
            .notna()
            .map(lambda x: "yes" if x else "no")
        )
        merged_map[str(table_name)] = merged

    return merged_map
