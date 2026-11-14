"""从已确认 Dataset 的 source 提取输出列，不重新发现或命名 Dataset。

物理 source 直接读取当前源元数据；SQL source 只接受可由当前源元数据
解析的查询投影。无法确定输出时明确失败，不回退成所有输入表的列。
"""

from __future__ import annotations

import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope, traverse_scope

from ..snapshots import TableSnapshot


def _text(value) -> str | None:
    return None if value is None or pd.isna(value) else str(value).strip() or None


def _table_key(table: exp.Table, dialect: str, *, normalize: bool) -> tuple[str, ...]:
    relation = table.copy()
    if normalize:
        normalize_identifiers(relation, dialect=dialect)
    return tuple(part.name for part in relation.parts)


class _SourceTables:
    """只在传入的当前 datasource 快照里解析物理表；短名必须唯一。"""

    def __init__(self, tables: TableSnapshot, dialect: str):
        self.tables = tables
        self.dialect = dialect
        self.keys: dict[tuple[str, ...], set[str]] = {}
        names = tables.table_meta["table_name"].astype(str)
        self.ambiguous_names = set(names[names.duplicated()])
        for row in tables.table_meta.to_dict("records"):
            name = str(row["table_name"])
            fullname = _text(row.get("table_fullname")) or name
            relation = sqlglot.parse_one(fullname, read=dialect, into=exp.Table)
            schema = _text(row.get("table_schema"))
            if schema and not relation.db:
                relation.set("db", exp.to_identifier(schema, quoted=True))
            key = _table_key(relation, dialect, normalize=False)
            self.keys.setdefault(key, set()).add(name)

    def columns(
        self, table: exp.Table, *, physical_source: bool = False
    ) -> pd.DataFrame:
        # 物理 source 可能是元数据原样写出的混合大小写名字；先精确匹配。
        # SQL 内的表引用则始终遵循方言的标识符大小写规则。
        key = _table_key(table, self.dialect, normalize=not physical_source)
        matches = self.keys.get(key, set())
        if not matches and len(key) == 1:
            matches = {
                name
                for parts, names in self.keys.items()
                if parts[-1:] == key
                for name in names
            }
        if not matches and physical_source:
            return self.columns(table)
        if len(matches) != 1:
            reason = (
                "找不到物理表" if not matches else "物理表短名不唯一，请指定 schema"
            )
            raise ValueError(f"{reason}: {table.sql(dialect=self.dialect)}")
        name = next(iter(matches))
        if name in self.ambiguous_names:
            raise ValueError(f"物理快照中表名重复，无法唯一确定列元数据: {name}")
        columns = self.tables.column_meta_of(name)
        if columns.empty:
            raise ValueError(f"物理表缺少列元数据: {table.sql(dialect=self.dialect)}")
        names = columns["column_name"].astype(str)
        if names.str.strip().eq("").any() or names.duplicated().any():
            raise ValueError(f"物理表列名为空或重复: {table.sql(dialect=self.dialect)}")
        return columns.copy()


def _passthrough_column(node: Node, columns: dict[str, dict[str, dict]]) -> dict:
    """仅继承单一列的直通血缘；计算表达式、UNION 等不猜测类型或注释。"""
    while not isinstance(node.expression, exp.Table):
        expression = node.expression
        if isinstance(expression, exp.Alias):
            expression = expression.this
        if not isinstance(expression, exp.Column) or len(node.downstream) != 1:
            return {}
        node = node.downstream[0]
    column = sqlglot.parse_one(node.name, into=exp.Column).name
    return columns.get(node.expression.name, {}).get(column, {})


def dataset_source_columns(
    source: str,
    tables: TableSnapshot,
    *,
    dialect: str = "postgres",
) -> pd.DataFrame:
    """返回 source 的输出列。SQL 只做静态解析，不执行用户 SQL。

    SQL 计算列需要显式别名，未知类型保留为空；SQL 不继承物理主键或
    非空约束（JOIN / 聚合等可能改变它们），这些由用户审核。
    """
    source = source.strip()
    if not source:
        raise ValueError("Dataset source 不能为空")
    try:
        statements = [
            item for item in sqlglot.parse(source, read=dialect) if item is not None
        ]
        if len(statements) != 1:
            raise ValueError("source 必须是物理表名或单条 SELECT 查询")
        query = statements[0]
        catalog = _SourceTables(tables, dialect)
        if not isinstance(query, exp.Query):
            table = sqlglot.parse_one(source, read=dialect, into=exp.Table)
            if any(not isinstance(part, exp.Identifier) for part in table.parts) or any(
                value
                for key, value in table.args.items()
                if key not in {"this", "db", "catalog"}
            ):
                raise ValueError("物理 source 只能是表名，不能带别名或其他操作")
            return catalog.columns(table, physical_source=True)

        if query.find(exp.DML, exp.DDL) is not None or query.find(exp.Into) is not None:
            raise ValueError("SQL source 只支持只读 SELECT 查询")
        query = query.unnest()
        # 将物理关系在内部 AST 中映射到单层名字，避免混合 schema 深度；
        # 保留查询别名，CTE / 子查询仍交由 SQLGlot 的作用域分析处理。
        schema: dict[str, dict[str, str]] = {}
        column_rows: dict[str, dict[str, dict]] = {}
        used_names = {
            identifier.name.lower() for identifier in query.find_all(exp.Identifier)
        }
        for scope in traverse_scope(query):
            # 包含子查询，避免 SELECT * 把优化器生成的 _col_0 当作真实输出名。
            for index, projection in enumerate(scope.expression.selects):
                if not isinstance(
                    projection, (exp.Column, exp.Star, exp.Alias)
                ) and index >= len(scope.outer_columns):
                    raise ValueError(
                        "SQL 计算列必须提供显式别名 AS，以确定 dataset_field 名称"
                    )
            for _, relation in scope.selected_sources.values():
                if not isinstance(relation, exp.Table):
                    continue
                columns = catalog.columns(relation)
                number = len(schema)
                internal_name = f"__dataset_source_{number}"
                while internal_name in used_names:
                    number += 1
                    internal_name = f"__dataset_source_{number}"
                used_names.add(internal_name)
                rows = {
                    str(row["column_name"]): row for row in columns.to_dict("records")
                }
                column_rows[internal_name] = rows
                schema[internal_name] = {
                    exp.to_identifier(name, quoted=True).sql(dialect=dialect): "UNKNOWN"
                    for name in rows
                }
                if not relation.alias:
                    relation.set("alias", exp.TableAlias(this=relation.this.copy()))
                relation.set("this", exp.to_identifier(internal_name))
                relation.set("db", None)
                relation.set("catalog", None)

        # 新版 sqlglot（OSS 锁定 30.23）已移除 qualify 的 infer_schema 参数且不再
        # 推断缺失 schema，行为等同旧版 infer_schema=False；传该参数会 TypeError。
        query = qualify(query, dialect=dialect, schema=schema)
        for union in query.find_all(exp.SetOperation):
            if len(union.this.selects) != len(union.expression.selects):
                raise ValueError("SQL 集合运算两侧的输出列数量不一致")
        output_names = [projection.alias_or_name for projection in query.selects]
        if not output_names or any(not name or name == "*" for name in output_names):
            raise ValueError("无法确定 SQL 的输出列")
        if len(output_names) != len(set(output_names)):
            raise ValueError("SQL 输出列名重复，请使用不同的 AS 别名")

        scope = build_scope(query)
        result = []
        for name in output_names:
            # 新版 sqlglot（30.23）的 lineage 必传 schema；已给 scope 时它只用于
            # PIVOT 列解析，传空映射与旧版缺省 schema=None 等价。
            inherited = _passthrough_column(
                lineage(
                    exp.column(name, quoted=True),
                    query,
                    schema={},
                    dialect=dialect,
                    scope=scope,
                ),
                column_rows,
            )
            result.append(
                {
                    "column_name": name,
                    "data_type": inherited.get("data_type"),
                    "column_comment": inherited.get("column_comment"),
                    "is_primary": "N",
                    "is_nullable": "Y",
                }
            )
        return pd.DataFrame(result)
    except (sqlglot.errors.SqlglotError, ValueError) as exc:
        raise ValueError(f"无法按 Dataset source 解析 dataset_field: {exc}") from exc
