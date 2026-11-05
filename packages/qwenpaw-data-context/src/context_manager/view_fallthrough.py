"""View-not-found 兜底重写的纯函数原子（PG 兼容错误文案）。

当前导出：
- ``FallthroughGiveUp`` / ``is_view_not_found_error`` / ``extract_cm_view_refs`` /
  ``rewrite_cm_view_to_physical``：cm_view 兜底重写的纯函数原子。
- ``extract_missing_table_name`` / ``rewrite_dataset_to_physical``：view 未建时按 dataset
  name 回退到物理表重写的纯函数原子。
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


class FallthroughGiveUp(RuntimeError):
    """rewrite_cm_view_to_physical 无法重写：multi-parent / zero-parent / 图里查不到。"""


def is_view_not_found_error(msg: object) -> bool:
    return isinstance(msg, str) and bool(
        re.search(
            r"42P01|does not exist|doesn't exist|not found",
            msg,
            re.IGNORECASE,
        )
    )


def _parse_query(sql: str, dialect: str | None):
    try:
        # Legacy callers did not supply a dialect. Retry MySQL only on parse failure.
        try:
            statements = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.ParseError:
            if dialect is not None:
                raise
            dialect = "mysql"
            statements = sqlglot.parse(sql, read=dialect)
        statements = [s for s in statements if s is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise FallthroughGiveUp("only one SELECT query can be rewritten")
        scopes = list(traverse_scope(statements[0]))
        if not scopes:
            raise FallthroughGiveUp("query scopes could not be resolved")
        for scope in scopes:
            _ = scope.selected_sources
        return statements[0], scopes, dialect
    except (sqlglot.errors.SqlglotError, ValueError) as exc:
        raise FallthroughGiveUp("query could not be parsed safely") from exc


def rewrite_table_references(sql: str, resolve, *, dialect: str | None = None) -> str:
    """Resolve physical source nodes once; literals, CTE names and columns stay intact.

    ``resolve(Table)`` returns a target Table or None. Qualified column references
    retain their binding through an alias when their source table is renamed.
    """
    tree, scopes, dialect = _parse_query(sql, dialect)
    changes = {}
    for scope in scopes:
        for _, source in scope.selected_sources.values():
            if not isinstance(source, exp.Table) or id(source) in changes:
                continue
            target = resolve(source)
            if target is not None and source.parts != target.parts:
                changes[id(source)] = (source, source.copy(), target)
    if not changes:
        return sql
    for scope in scopes:
        for column in [*scope.columns, *scope.stars]:
            if not column.table:
                continue
            owner = scope
            source = None
            while owner is not None:
                if column.table in owner.sources:
                    source = owner.sources[column.table]
                    break
                owner = owner.parent
            change = changes.get(id(source))
            if change is None:
                continue
            table, original, _ = change
            if column.db and column.db != original.db:
                continue
            if column.catalog and column.catalog != original.catalog:
                continue
            if not original.alias:
                table.set("alias", exp.TableAlias(this=original.this.copy()))
            # Only the qualifier changes; the selected column identifier is preserved.
            column.set("db", None)
            column.set("catalog", None)
    for source, _, target in changes.values():
        for part in ("this", "db", "catalog"):
            value = target.args.get(part)
            source.set(part, value.copy() if value is not None else None)
    return tree.sql(dialect=dialect)


def _physical_table(parent: tuple[str, str]) -> exp.Table:
    schema, table = parent
    return exp.Table(
        this=exp.Identifier(this=table, quoted=True),
        db=exp.Identifier(this=schema, quoted=True) if schema else None,
    )


def is_direct_table_projection(
    sql: str, target: exp.Table, *, dialect: str | None = None
) -> bool:
    """A filtered/aggregated view cannot safely fall back to its parent table."""
    if not sql.strip():
        return True  # Mapping-only datasets have no declared SQL transformation.
    try:
        tree, _, _ = _parse_query(sql, dialect)
        if not isinstance(tree, exp.Select):
            return False
        if any(
            value
            for key, value in tree.args.items()
            if key not in {"expressions", "from_"}
        ):
            return False
        source = tree.args.get("from_")
        source = source.this if source else None
        if not isinstance(source, exp.Table) or len(tree.expressions) != 1:
            return False
        star = tree.expressions[0]
        if not (isinstance(star, exp.Star) and not any(star.args.values())):
            return False
        return tuple(p.name for p in source.parts) == tuple(
            p.name for p in target.parts
        )
    except FallthroughGiveUp:
        return False


def extract_cm_view_refs(sql: str, *, dialect: str | None = None) -> set[str]:
    _, scopes, _ = _parse_query(sql, dialect)
    return {
        source.name
        for scope in scopes
        for _, source in scope.selected_sources.values()
        if isinstance(source, exp.Table) and source.db.casefold() == "cm_view"
    }


def rewrite_cm_view_to_physical(
    sql: str,
    dataset_to_tables: dict[str, list[tuple[str, str]]],
    *,
    dialect: str | None = None,
) -> str:
    refs = extract_cm_view_refs(sql, dialect=dialect)
    if not refs:
        raise FallthroughGiveUp("no cm_view refs to rewrite")
    targets = {}
    for name in refs:
        parents = dataset_to_tables.get(name) or []
        if len(parents) != 1:
            raise FallthroughGiveUp(f"dataset {name!r} has {len(parents)} parents")
        targets[name] = _physical_table(parents[0])
    return rewrite_table_references(
        sql,
        lambda t: targets.get(t.name) if t.db.casefold() == "cm_view" else None,
        dialect=dialect,
    )


# ---------------------------------------------------------------------- #
# view-not-found fallthrough: dataset name → physical table
# ---------------------------------------------------------------------- #

_MISSING_REL_RE = re.compile(
    r"relation\s+"
    r"(?:"
    r'"[^"]*"\."([^"]+)"'  # "schema"."table"
    r"|"
    r'"([^"]+)"'  # "table" (no schema)
    r"|"
    r"[A-Za-z_]\w*\.([A-Za-z_]\w*)"  # schema.table (bare)
    r"|"
    r"([A-Za-z_]\w*)"  # table (bare, no schema, no dots)
    r")"
    r"\s+does not exist",
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
        mysql = re.search(
            r"Table ['\"](?:[^'\"]*\.)?([^.'\"]+)['\"] doesn't exist",
            error_msg,
            re.IGNORECASE,
        )
        duckdb = re.search(
            r"Table with name ([^\s]+) does not exist", error_msg, re.IGNORECASE
        )
        match = mysql or duckdb
        return match.group(1) if match else None
    # 4 个捕获组，取第一个非 None 的
    name = next((g for g in m.groups() if g is not None), None)
    return name.rsplit(".", 1)[-1] if name else None


def rewrite_dataset_to_physical(
    sql: str,
    name: str,
    parent: tuple[str, str],
    *,
    dialect: str | None = None,
) -> str:
    """Replace a dataset table reference, excluding CTE aliases and non-table text."""
    target = _physical_table(parent)
    return rewrite_table_references(
        sql,
        lambda t: target if t.name == name else None,
        dialect=dialect,
    )
