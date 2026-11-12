from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

from .utils_ast import SqlAnalysis

logger = logging.getLogger(__name__)

DIALECT = SqlAnalysis.DEFAULT_DIALECT

#: dataset 模板至少需要的投影列数；``SELECT *`` 不受此限制。
DATASET_MIN_SELECT_COLUMNS = 5


# ===================================================================
#  基础组件，用于模板化 SQL 解析
# ===================================================================
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


# ===================================================================
#  结构模板 & 判重 key
# ===================================================================


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
    dialect: str = DIALECT,
) -> tuple[str, str]:
    """对每个 Select 的 WHERE/HAVING 做字面量占位，返回 ``(norm_key, template)``。

    输出的结果示例::

        ("9f1c...e7", "select date_id, sum(gmv) from ads_..._1d where ds = ? group by date_id")

    ``norm_key`` 是 ``template`` 的 sha256，结构等价（只有筛选字面量不同）的 SQL
    会得到同一个 key，用于统计重复查询模式。

    ``root`` 既可以是整条 SQL 的根节点，也可以是其中某一层 ``Select``；嵌套查询
    应逐层调用并各自计入重复统计，见 :func:`iter_dataset_occurrences`。
    """
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
    """是否保留该 Select 作为 dataset 模板。

    保留 ``SELECT *``，或投影表达式数量 ``>= min_columns``；
    否则删除（如 ``SELECT MAX(ds) FROM ...`` 这类过窄查询）。
    """

    def _is_star_projection(node: exp.Expression) -> bool:
        """是否为 ``*`` / ``t.*`` 形式的投影项。"""
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
    return len(list(select.expressions or [])) >= min_columns


def is_dataset_sql_kept(
    sql: str,
    dialect: str = DIALECT,
    *,
    min_columns: int = DATASET_MIN_SELECT_COLUMNS,
) -> bool:
    """对 SQL 文本做 AST 判定，规则同 :func:`is_dataset_select_kept`。"""
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


def filter_dataset_records(
    frame: pd.DataFrame,
    dialect: str = DIALECT,
    *,
    min_columns: int = DATASET_MIN_SELECT_COLUMNS,
) -> pd.DataFrame:
    """按 AST 规则筛掉过窄的 dataset 模板行（依赖 ``repeated_sql`` 列）。"""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return (
            frame.copy()
            if isinstance(frame, pd.DataFrame)
            else pd.DataFrame(columns=["parents", "repeated_sql", "count"])
        )
    if "repeated_sql" not in frame.columns:
        return frame.copy()
    mask = frame["repeated_sql"].map(
        lambda sql: is_dataset_sql_kept(
            str(sql), dialect=dialect, min_columns=min_columns
        )
    )
    kept = int(mask.sum())
    dropped = int(len(frame) - kept)
    if dropped:
        logger.info(
            "dataset templates: 按投影宽度筛除 %s 条（保留 SELECT * 或列数>=%s），剩余 %s",
            dropped,
            min_columns,
            kept,
        )
    return frame.loc[mask].reset_index(drop=True)


def iter_dataset_occurrences(analysis: SqlAnalysis) -> list[dict[str, Any]]:
    """把一条 SQL 展开成「根查询 + 每一层嵌套子查询」的多条 dataset 出现记录。

    直接复用 :attr:`SqlAnalysis.select_exprs`——它已经穿透 FROM 子查询、CTE body
    与 scalar/EXISTS 子查询，收录了 AST 中全部 ``Select``。每层各算一次
    ``norm_key``，因此内层子查询能与其它日志里结构等价的查询合并计数，而不会因为
    只看最外层被漏掉。

    过窄投影（非 ``SELECT *`` 且列数小于 :data:`DATASET_MIN_SELECT_COLUMNS`）
    会被丢弃，避免 ``SELECT MAX(ds)`` 之类进入候选。

    输出的结果示例（三层嵌套的 SQL 产出 3 条）::

        [{"norm_key": "52e1...", "repeated_sql": "SELECT AVG(...) FROM (...)",
          "parents": "[\"public.dws_..._1d\"]"},
         {"norm_key": "63c8...", "repeated_sql": "SELECT ds, SUM(...) FROM (...) GROUP BY ds", ...},
         {"norm_key": "8abd...", "repeated_sql": "SELECT models_name, ... FROM public.dws_..._1d WHERE ...", ...}]

    列说明：
        - ``norm_key``：该层 Select 的结构判重 key；
        - ``repeated_sql``：该层 Select 按 dialect 渲染后的 SQL；
        - ``parents``：该层 Select 的来源物理表，JSON 数组字符串。

    Corner cases：
        - 无 Select 的 AST 返回空列表；
        - 某层无法解析出来源表时 ``parents`` 为 ``"[]"``。
    """
    dialect = analysis.dialect_name
    rows: list[dict[str, Any]] = []
    for select in analysis.select_exprs:
        if not is_dataset_select_kept(select):
            continue
        norm_key, _ = norm_key_and_template_from_root(select, dialect=dialect)
        rows.append(
            {
                "norm_key": norm_key,
                "repeated_sql": select.sql(dialect=dialect),
                "parents": json.dumps(
                    sorted(analysis.resolve_select_source_table_names(select)),
                    ensure_ascii=False,
                ),
            }
        )
    return rows


# ===================================================================
#  LLM 输出校验与合并（供 DatasetTaskHandler 使用）
# ===================================================================


def validate_dataset_meta_items(
    items: list[dict[str, Any]],
    expected_sqls: set[str],
) -> list[dict[str, Any]]:
    """校验 LLM 生成的视图元信息，保证每条输入 SQL 都被覆盖。

    输出的结果示例（``list[dict]``，字段归一化为 str）::

        [{"sql": "SELECT date_id, SUM(gmv) FROM ...",
          "parents": "[\"public.ads_..._1d\"]",
          "name": "revenue_daily_overview",
          "description": "按日汇总的收入总览，用于经营日报"}]

    Corner cases：
        - 不是数组、元素不是对象、元素缺 ``sql`` 字段时抛 ``ValueError``；
        - ``expected_sqls`` 有遗漏时抛 ``ValueError``（由 handler 兜底为空结果）。
    """
    if not isinstance(items, list):
        raise ValueError("dataset metadata 输出必须是数组")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("dataset metadata 数组元素必须是对象")

        sql = str(item.get("sql", "")).strip()
        parents = str(item.get("parents", "")).strip()
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()

        if not sql:
            raise ValueError("dataset metadata 元素缺少 sql 字段")

        normalized.append(
            {
                "sql": sql,
                "parents": parents,
                "name": name,
                "description": description,
            }
        )
        seen.add(sql)

    missing = expected_sqls - seen
    if missing:
        raise ValueError(f"以下 SQL 未生成 metadata: {len(missing)} 条")

    return normalized


def merge_dataset_views(
    df: pd.DataFrame,
    views: list[dict[str, Any]],
) -> pd.DataFrame:
    """将 LLM 生成的命名与描述合并回原始聚合结果。

    输入 ``df`` 为 ``parents,repeated_sql,count``，输出在其右侧追加两列::

        parents,repeated_sql,count,name,description
        "[""public.ads_..._1d""]",SELECT date_id...,124,revenue_daily_overview,按日汇总的收入总览

    Corner cases：
        - 未被 LLM 覆盖的 SQL 两列填空串；``df`` 为空时原样返回副本。
    """
    if df.empty:
        return df.copy()

    view_map = {item["sql"]: item for item in views}
    enriched = df.copy()
    enriched["name"] = enriched["repeated_sql"].map(
        lambda sql: view_map.get(str(sql), {}).get("name", "")
    )
    enriched["description"] = enriched["repeated_sql"].map(
        lambda sql: view_map.get(str(sql), {}).get("description", "")
    )
    return enriched
