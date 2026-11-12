"""JOIN 边抽取。供 DOMAIN —— 一起被 JOIN 的表大概率属于同一业务域。

移植自 ``profiling/algorithm/profiler_biz_domain.py`` 的
``extract_join_edges`` + ``aggregate_join_evidence`` 的边聚合部分。
并查集聚类（clusters / table_frequencies）**不在这里**——snapshot 只存边表，
聚类是 DOMAIN 任务的下游职责。

与原版的唯一差异（方案 B）：表标识用 :func:`~.base.short_table_name`
（原版 ``_normalize_table_name`` 只剥 schema，这里再剥分区后缀）。
由此，同一逻辑表两个分区之间的 JOIN 会按自连接丢弃——分区间自 JOIN
不构成跨表关联信息，语义与原版对自连接的处理一致。
"""

from __future__ import annotations

import pandas as pd
from sqlglot import exp

from ..snapshots.query_log import REQUIRED_EVIDENCE
from .base import rows_to_frame, short_table_name
from .sql_analysis import SqlAnalysis


class JoinParser:
    """按 ``(左表, 右表, 连接类型, ON 列)`` 聚合 JOIN 边并计数。

    Corner cases（忠实原版）：

    - 只拆 ON 的顶层 AND 等值条件，``OR`` 子树整体跳过（拆开会产生不成立
      的证据）；
    - 两侧列都必须**跨作用域唯一**归属到物理表（穿透 CTE / 派生表），
      否则整条丢弃——宁可丢证据也不归错表；
    - 自连接（左右同表）丢弃——它不提供跨表关联信息；
    - 同一条 SQL 里重复的边只计一次（per-SQL 去重在 :meth:`visit` 内），
      ``count`` = 出现该边的 SQL 条数。
    """

    kind = "join"

    def __init__(self) -> None:
        self._rows: list[dict[str, str]] = []

    def visit(self, analysis: SqlAnalysis) -> None:
        seen: set[tuple[str, str, str, str]] = set()

        for join in analysis.join_exprs:
            on_expr = join.args.get("on")
            if on_expr is None:
                continue
            parent_select = analysis.resolve_owning_select_expr(join)
            if parent_select is None:
                continue

            join_type_parts: list[str] = []
            if side := join.args.get("side"):
                join_type_parts.append(str(side).upper())
            if kind := join.args.get("kind"):
                join_type_parts.append(str(kind).replace("_", " ").upper())
            join_type = (
                f"{' '.join(join_type_parts)} JOIN" if join_type_parts else "JOIN"
            )
            table_pairs: dict[tuple[str, str], list[str]] = {}

            # 顶层 AND 中的等值条件分别贡献 JOIN 证据；OR 子树不做不安全拆分。
            for part in analysis.extract_conjunct_exprs(on_expr):
                if not isinstance(part, exp.EQ):
                    continue
                left_col, right_col = part.left, part.right
                if not isinstance(left_col, exp.Column) or not isinstance(
                    right_col, exp.Column
                ):
                    continue

                left_source = analysis.resolve_unique_column_source_table_name(
                    parent_select, left_col
                )
                right_source = analysis.resolve_unique_column_source_table_name(
                    parent_select, right_col
                )
                if left_source is None or right_source is None:
                    continue

                left_table = short_table_name(left_source, analysis.dialect_name)
                right_table = short_table_name(right_source, analysis.dialect_name)
                if not left_table or not right_table or left_table == right_table:
                    continue

                pair = tuple(sorted((left_table, right_table)))
                on_column = (
                    f"{analysis.extract_column_name(left_col)}="
                    f"{analysis.extract_column_name(right_col)}"
                )
                table_pairs.setdefault(pair, []).append(on_column)

            for (left_table, right_table), on_columns in table_pairs.items():
                columns_text = ",".join(sorted(set(on_columns)))
                key = (left_table, right_table, join_type, columns_text)
                if key in seen:
                    continue
                seen.add(key)

        self._rows.extend(
            {
                "left_table": left,
                "right_table": right,
                "join_type": join_type,
                "on_columns": on_columns,
            }
            for left, right, join_type, on_columns in seen
        )

    def result(self) -> pd.DataFrame:
        columns = REQUIRED_EVIDENCE["join"]
        if not self._rows:
            return rows_to_frame([], columns)
        return (
            pd.DataFrame(self._rows)
            .groupby(
                ["left_table", "right_table", "join_type", "on_columns"],
                as_index=False,
            )
            .size()
            .rename(columns={"size": "count"})
        )[list(columns)]


__all__ = ["JoinParser"]
