from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd
from sqlglot import exp

from .utils_ast import SqlAnalysis
from .data_context import SourceTableManager

logger = logging.getLogger(__name__)


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
    row = {
        "dimension_name": col,
        "table_name": table_name,
        "source": source,
    }
    rows.append(row)


def group_dimensions_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
) -> list[dict[str, Any]]:
    """从一个 SELECT 的 GROUP BY 提取维度列。

    输出的结果示例（``list[dict]``，键为 dimension_name/table_name/source）::

        [{"dimension_name": "date_id", "table_name": "ads_..._1d", "source": "group_by"},
         {"dimension_name": "general_type", "table_name": "ads_..._1d", "source": "group_by"}]

    Corner cases：
        - 列无法唯一定位到物理表时整行丢弃（``table_name is None``）；
        - ``GROUP BY 1, 2`` 这类序号引用不含 Column 节点，自然不产出行。
    """
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
                analysis.resolve_unique_column_source_table_name(
                    select,
                    col_expr,
                ),
                analysis.extract_column_name(col_expr),
                "group_by",
            )
    return rows


def where_dimensions_from_select(
    analysis: SqlAnalysis,
    select: exp.Select,
) -> list[dict[str, Any]]:
    """从一个 SELECT 的 WHERE 提取维度列（仅 ``col = 字面量`` 与 ``col IN (...)``）。

    输出的结果示例，与 :func:`group_dimensions_from_select` 同构，``source`` 恒为
    ``where``::

        [{"dimension_name": "type_level1", "table_name": "ads_..._1d", "source": "where"}]

    Corner cases：
        - 只拆顶层 AND，OR 子树不做不安全拆分；
        - ``col > 100`` / ``col LIKE ...`` 等非等值谓词不算维度使用。
    """
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
            analysis.resolve_unique_column_source_table_name(
                select,
                col_expr,
            ),
            analysis.extract_column_name(col_expr),
            "where",
        )
    return rows


def merge_dimension_evidence_into_tables(
    records: pd.DataFrame | None,
    table_map: Mapping[str, pd.DataFrame] | SourceTableManager,
) -> dict[str, pd.DataFrame]:
    """把日志维度证据 left-join 进各表列 DataFrame。

    ``records`` 为 :meth:`QueryLogContext._extract_dimension_records` 的输出，
    即 ``dimension_name,table_name,count,sources``；``table_map`` 是
    ``{table_name: 列详情}``。

    输出 ``{table_name: 列详情 DataFrame}``，在原列详情右侧追加三列，示例::

        column_name,data_type,column_comment,...,query_log_clause_count,query_log_clause_type,is_dimension_in_query_logs
        date_id,text,数据日期,...,21.0,"['group_by', 'where']",yes
        gmv,numeric,成交额,...,,,no

    维度列必须已存在于原始表：只更新已有 ``column_name``，绝不新增行。
    对所有表都产出结果；未命中日志的列 ``is_dimension_in_query_logs=no``。

    Corner cases：
        - ``records`` 为 ``None`` / 空表时所有列都标 ``no``；
        - 重复调用幂等：三列若已存在会先 drop 再重新合并；
        - 空的或非 DataFrame 的表会被跳过并 warning，不出现在返回值里。
    """
    from collections.abc import Mapping as MappingABC

    if isinstance(table_map, SourceTableManager):
        items = list(table_map.items())
    elif isinstance(table_map, MappingABC):
        items = list(table_map.items())
    else:
        items = []

    empty_lookup = pd.DataFrame(
        columns=["column_name", "query_log_clause_count", "query_log_clause_type"]
    )
    if records is None or not isinstance(records, pd.DataFrame) or records.empty:
        lookup = empty_lookup
    else:
        lookup = records.copy()
        rename = {}
        if "dimension_name" in lookup.columns:
            rename["dimension_name"] = "column_name"
        if "sources" in lookup.columns:
            rename["sources"] = "query_log_clause_type"
        if "count" in lookup.columns:
            rename["count"] = "query_log_clause_count"
        if rename:
            lookup = lookup.rename(columns=rename)
        for col in ("table_name", "column_name"):
            if col in lookup.columns:
                lookup[col] = lookup[col].astype(str)
        keep = [
            c
            for c in (
                "table_name",
                "column_name",
                "query_log_clause_count",
                "query_log_clause_type",
            )
            if c in lookup.columns
        ]
        lookup = lookup[keep] if keep else empty_lookup

    merged_map: dict[str, pd.DataFrame] = {}
    for table_name, table_df in items:
        if table_df is None or not isinstance(table_df, pd.DataFrame) or table_df.empty:
            logger.warning(f"Skip invalid or empty table dataframe: {table_name}")
            continue
        current = table_df.copy()
        current["column_name"] = current["column_name"].astype(str)
        if "table_name" in lookup.columns and not lookup.empty:
            table_lookup = lookup[lookup["table_name"] == str(table_name)][
                ["column_name", "query_log_clause_count", "query_log_clause_type"]
            ].copy()
        else:
            table_lookup = empty_lookup
        # drop prior evidence cols if re-merging
        drop_cols = [
            c
            for c in (
                "query_log_clause_count",
                "query_log_clause_type",
                "is_dimension_in_query_logs",
            )
            if c in current.columns
        ]
        if drop_cols:
            current = current.drop(columns=drop_cols)
        merged = current.merge(table_lookup, on="column_name", how="left")
        merged["is_dimension_in_query_logs"] = (
            merged["query_log_clause_count"].notna().map(lambda x: "yes" if x else "no")
        )
        merged_map[str(table_name)] = merged
    return merged_map
