"""SQL 模板抽取。供 DATASET —— 高频重复的查询模式就是语义视图的候选。

原版关键行为（全部保留）：

- **宽表过滤**：只保留全 ``*`` 投影或投影数 >= 5 的 Select，
  ``SELECT MAX(ds)`` / ``SELECT DISTINCT col`` 这类过窄探测查询不进候选；
- 每条 SQL 展开成「根查询 + 每一层嵌套子查询」分别判重计数；
- 规范化只动**比较位**的筛选字面量（EQ/NEQ/GT/GTE/LT/LTE 两侧、BETWEEN
  low/high、IN 列表项、LIKE/ILIKE 右侧），SELECT 列表与 GROUP BY 是模板的
  语义骨架，不动；
- ``norm_key`` = 按 dialect 渲染 + 小写 + 空白折叠后的 sha256；
- ``count`` 不做 per-SQL 去重：同一条日志里出现两次等价 Select 计 2。

与原版的唯一差异（方案 B）：``parents`` 逐段走短名归一（原版带 schema）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

from ..snapshots.query_log import REQUIRED_EVIDENCE
from .base import rows_to_frame, short_table_name
from .sql_analysis import SqlAnalysis

#: dataset 模板至少需要的投影列数；``SELECT *`` 不受此限制。
DATASET_MIN_SELECT_COLUMNS = 5


# ===========================================================================
# 基础组件：字面量占位（仅比较位）
# ===========================================================================


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _placeholder() -> exp.Expression:
    return exp.convert("?")


def _is_filter_literal(node: exp.Expression | None) -> bool:
    return isinstance(node, (exp.Literal, exp.Boolean))


def _replace_literal(node: exp.Expression | None) -> exp.Expression | None:
    if _is_filter_literal(node):
        return _placeholder()
    return node


def _normalize_comparison(node: exp.Expression) -> exp.Expression:
    new = node.copy()
    if _is_filter_literal(new.args.get("expression")):
        new.set("expression", _placeholder())
    if _is_filter_literal(new.args.get("this")):
        new.set("this", _placeholder())
    return new


def _normalize_between(node: exp.Between) -> exp.Between:
    new = node.copy()
    if _is_filter_literal(new.args.get("low")):
        new.set("low", _placeholder())
    if _is_filter_literal(new.args.get("high")):
        new.set("high", _placeholder())
    return new


def _normalize_in(node: exp.In) -> exp.In:
    new = node.copy()
    if new.expressions:
        new.set(
            "expressions",
            [_replace_literal(item) or item for item in new.expressions],
        )
    return new


def _normalize_like(node: exp.Expression) -> exp.Expression:
    new = node.copy()
    if _is_filter_literal(new.args.get("expression")):
        new.set("expression", _placeholder())
    return new


def normalize_predicate(expr: exp.Expression) -> exp.Expression:
    """递归改写 WHERE/HAVING 谓词：仅替换比较/范围/IN/LIKE 上的筛选字面量。"""
    if isinstance(expr, exp.And):
        return exp.And(
            this=normalize_predicate(expr.this),
            expression=normalize_predicate(expr.expression),
        )
    if isinstance(expr, exp.Or):
        return exp.Or(
            this=normalize_predicate(expr.this),
            expression=normalize_predicate(expr.expression),
        )
    if isinstance(expr, exp.Not):
        return exp.Not(this=normalize_predicate(expr.this))
    if isinstance(expr, exp.Paren):
        return exp.Paren(this=normalize_predicate(expr.this))
    if isinstance(expr, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return _normalize_comparison(expr)
    if isinstance(expr, exp.Between):
        return _normalize_between(expr)
    if isinstance(expr, exp.In):
        return _normalize_in(expr)
    if isinstance(expr, (exp.Like, exp.ILike)):
        return _normalize_like(expr)
    return expr


# ===========================================================================
# 结构模板 & 判重 key
# ===========================================================================


def _normalize_filter_clause(clause: exp.Expression | None) -> exp.Expression | None:
    if clause is None:
        return None
    body = clause.this if isinstance(clause, (exp.Where, exp.Having)) else clause
    normalized = normalize_predicate(body)
    if isinstance(clause, exp.Where):
        return exp.Where(this=normalized)
    if isinstance(clause, exp.Having):
        return exp.Having(this=normalized)
    return normalized


def norm_key_and_template_from_root(
    root: exp.Expression,
    dialect: str,
) -> tuple[str, str]:
    """对每个 Select 的 WHERE/HAVING 做字面量占位，返回 ``(norm_key, template)``。"""
    normalized = root.copy()
    for select in normalized.find_all(exp.Select):
        for clause_name in ("where", "having"):
            clause = select.args.get(clause_name)
            if clause is None:
                continue
            select.set(clause_name, _normalize_filter_clause(clause))
    template = normalize_whitespace(normalized.sql(dialect=dialect))
    norm_key = hashlib.sha256(template.encode("utf-8")).hexdigest()
    return norm_key, template


def is_dataset_select_kept(
    select: exp.Select,
    *,
    min_columns: int = DATASET_MIN_SELECT_COLUMNS,
) -> bool:
    """保留 ``SELECT *``，或投影表达式数量 ``>= min_columns`` 的 Select。"""

    def _is_star_projection(node: exp.Expression) -> bool:
        if isinstance(node, exp.Star):
            return True
        if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
            return True
        if isinstance(node, exp.Alias):
            return _is_star_projection(node.this)
        return False

    expressions = list(select.expressions or [])
    if bool(expressions) and all(_is_star_projection(item) for item in expressions):
        return True
    return len(expressions) >= min_columns


def _is_dataset_sql_kept(
    sql: str, dialect: str, min_columns: int = DATASET_MIN_SELECT_COLUMNS
) -> bool:
    """对渲染后的 SQL 文本复检宽表规则（原版末道防线，防方言渲染边界差异）。"""
    text = str(sql or "").strip()
    if not text:
        return False
    try:
        root = sqlglot.parse_one(text, read=dialect)
    except Exception:
        return False
    select = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if not isinstance(select, exp.Select):
        return False
    return is_dataset_select_kept(select, min_columns=min_columns)


class SqlTemplateParser:
    """按**结构等价**聚合重复查询，产出 ``parents / repeated_sql / count``。"""

    kind = "sql_template"

    def __init__(
        self,
        min_count: int = 10,
        min_columns: int = DATASET_MIN_SELECT_COLUMNS,
    ) -> None:
        self._min_count = max(1, int(min_count))
        self._min_columns = max(1, int(min_columns))
        self._rows: list[dict[str, Any]] = []
        self._dialect = SqlAnalysis.DEFAULT_DIALECT

    def visit(self, analysis: SqlAnalysis) -> None:
        dialect = self._dialect = analysis.dialect_name
        for select in analysis.select_exprs:
            if not is_dataset_select_kept(select, min_columns=self._min_columns):
                continue
            norm_key, _ = norm_key_and_template_from_root(select, dialect=dialect)
            parents = sorted(
                {
                    short
                    for name in analysis.resolve_select_source_table_names(select)
                    if (short := short_table_name(name, dialect))
                }
            )
            self._rows.append(
                {
                    "norm_key": norm_key,
                    "repeated_sql": select.sql(dialect=dialect),
                    "parents": json.dumps(parents, ensure_ascii=False),
                }
            )

    def result(self) -> pd.DataFrame:
        columns = REQUIRED_EVIDENCE["sql_template"]
        if not self._rows:
            return rows_to_frame([], columns)
        grouped = (
            pd.DataFrame(self._rows)
            .groupby("norm_key", as_index=False)
            .agg(
                count=("norm_key", "size"),
                repeated_sql=("repeated_sql", "first"),
                parents=("parents", "first"),
            )
        )
        filtered = (
            grouped[grouped["count"] >= self._min_count][list(columns)]
            .sort_values(["count", "repeated_sql"], ascending=[False, True])
            .reset_index(drop=True)
        )
        mask = filtered["repeated_sql"].map(
            lambda sql: _is_dataset_sql_kept(str(sql), self._dialect, self._min_columns)
        )
        return filtered.loc[mask].reset_index(drop=True)


__all__ = [
    "DATASET_MIN_SELECT_COLUMNS",
    "SqlTemplateParser",
    "is_dataset_select_kept",
    "norm_key_and_template_from_root",
    "normalize_predicate",
]
