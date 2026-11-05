"""View-not-found 兜底重写的纯函数原子（PG 兼容错误文案）。

当前导出：
- ``FallthroughGiveUp`` / ``is_view_not_found_error`` / ``extract_cm_view_refs`` /
  ``rewrite_cm_view_to_physical``：cm_view 兜底重写的纯函数原子。
- ``extract_missing_table_name`` / ``rewrite_dataset_to_physical``：view 未建时按 dataset
  name 回退到物理表重写的纯函数原子。
"""
from __future__ import annotations

import re


class FallthroughGiveUp(RuntimeError):
    """rewrite_cm_view_to_physical 无法重写：multi-parent / zero-parent / 图里查不到。"""


_CM_VIEW_REF_RE = re.compile(r"\bcm_view\.([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)

_VIEW_NOT_FOUND_PATTERNS = (
    re.compile(r"42P01"),
    re.compile(r"does not exist", re.IGNORECASE),
    re.compile(r"not found", re.IGNORECASE),
)


def is_view_not_found_error(msg: object) -> bool:
    """松匹配:PG SQLSTATE 42P01,或常见 'does not exist' / 'not found' 文案。"""
    if not isinstance(msg, str) or not msg:
        return False
    return any(p.search(msg) for p in _VIEW_NOT_FOUND_PATTERNS)


def extract_cm_view_refs(sql: str) -> set[str]:
    """从 SQL 文本里抓出所有 cm_view.<ident> 引用的 ident 名(去重)。"""
    return {m.group(1) for m in _CM_VIEW_REF_RE.finditer(sql)}


def rewrite_cm_view_to_physical(
    sql: str,
    dataset_to_tables: dict[str, list[tuple[str, str]]],
) -> str:
    """把 SQL 里的 cm_view.<name> 重写为对应物理表 <schema>.<name>。

    ``dataset_to_tables[name]`` 是该 dataset CONTAINS_TABLE 出去的 [(schema,table), ...]
    - 单 parent: 直接 re.sub 替换
    - 多 / 零 parent / 未知 name: 抛 FallthroughGiveUp,由外层抛回原始错误
    """
    refs = extract_cm_view_refs(sql)
    if not refs:
        raise FallthroughGiveUp("no cm_view refs to rewrite")
    mapping: dict[str, str] = {}
    for name in refs:
        tables = dataset_to_tables.get(name) or []
        if len(tables) != 1:
            raise FallthroughGiveUp(
                f"dataset {name!r} has {len(tables)} parents; cannot fallthrough"
            )
        schema, table = tables[0]
        mapping[name] = f"{schema}.{table}"

    def _sub(m: "re.Match[str]") -> str:
        return mapping.get(m.group(1), m.group(0))

    return _CM_VIEW_REF_RE.sub(_sub, sql)


# ---------------------------------------------------------------------- #
# view-not-found fallthrough: dataset name → physical table
# ---------------------------------------------------------------------- #

_MISSING_REL_RE = re.compile(
    r'relation\s+'
    r'(?:'
    r'"[^"]*"\."([^"]+)"'           # "schema"."table"
    r'|'
    r'"([^"]+)"'                    # "table" (no schema)
    r'|'
    r'[A-Za-z_]\w*\.([A-Za-z_]\w*)' # schema.table (bare)
    r'|'
    r'([A-Za-z_]\w*)'              # table (bare, no schema, no dots)
    r')'
    r'\s+does not exist',
    re.IGNORECASE,
)


def extract_missing_table_name(error_msg: str) -> str | None:
    """从 PG 'relation "X" does not exist' 错误消息中提取表名。

    处理以下格式：
    - ``relation "view_xxx" does not exist``
    - ``relation "public"."view_xxx" does not exist``
    - ``relation public.view_xxx does not exist``
    - ``relation view_xxx does not exist``
    - ``SQLSTATE 42P01 ... relation "view_xxx"``
    """
    if not isinstance(error_msg, str) or not error_msg:
        return None
    m = _MISSING_REL_RE.search(error_msg)
    if not m:
        return None
    # 4 个捕获组，取第一个非 None 的
    return next((g for g in m.groups() if g is not None), None)


def _qualified_name_res(name: str) -> list["re.Pattern[str]"]:
    """Patterns for qualified references to name in SQL.

    Covers db.name where any segment may be bare, backtick-quoted or
    double-quoted, plus the bare quoted name form. A bare-name
    substitution would turn db.name into the illegal three-part
    db.schema.table (MySQL 1064), so qualified references must be
    replaced whole, before the bare-name pass runs.
    """
    esc = re.escape(name)
    bt = chr(96)  # backtick
    dq = chr(34)  # double quote
    ident = "[A-Za-z_][A-Za-z0-9_$]*"
    seg = "(?:" + ident + "|" + bt + "[^" + bt + "]+" + bt + "|" + dq + "[^" + dq + "]+" + dq + ")"
    # 整段限定引用。用 lookahead 而非词边界收尾:闭引号被贪心吞掉后整体仍然
    # 成立,避免回溯丢掉右引号、留下游离的引号字符。
    return [
        re.compile(
            "(?<![\\w.$])" + seg + "\\s*\\.\\s*(?:"
            + bt + "?" + esc + bt + "?|" + dq + esc + dq
            + ")(?=$|[^A-Za-z0-9_$])",
            re.IGNORECASE,
        ),
        # 裸引号形式:name 带引号整体替换,避免裸名替换产出
        # 反引号包裹的 schema.table(库名被包进单个引号标识符,MySQL 1146)。
        re.compile(bt + esc + bt + "|" + dq + esc + dq, re.IGNORECASE),
    ]

def rewrite_dataset_to_physical(
    sql: str,
    name: str,
    parent: tuple[str, str],
) -> str:
    """把 SQL 中对 dataset ``name`` 的引用替换为物理表 ``<schema>.<table>``。

    同时处理 quoted（``"view_xxx"``）和 bare（``view_xxx``）两种写法。

    - ``parent`` 是单个 ``(schema, table)`` 元组
    - 多 parent / 零 parent 由调用方在传入前检查,这里只负责替换
    """
    schema, table = parent
    replacement = f"{schema}.{table}"

    # 限定引用（db.name / `db`.`name` / "db"."name"）优先匹配：整段替换为
    # schema.table，避免裸名替换把 "db.name" 变成 "db.schema.table" 三段式
    # 非法标识符（MySQL 1064）。
    for pat in _qualified_name_res(name):
        sql = pat.sub(replacement, sql)
    # quoted identifiers: "name" → schema.table
    sql = re.sub(
        r'"' + re.escape(name) + r'"',
        replacement,
        sql,
    )
    # bare name with word boundary
    sql = re.sub(
        r"\b" + re.escape(name) + r"\b",
        replacement,
        sql,
    )
    return sql
