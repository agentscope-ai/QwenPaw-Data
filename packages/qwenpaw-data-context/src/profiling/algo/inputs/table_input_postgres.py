"""PostgreSQL / Hologres 的物理表来源。

Preserves physical query targets when date-suffixed tables are grouped.
Concurrency and probe budgets are managed by :class:`~.table_input_sql.SqlTableInput`.
"""

from __future__ import annotations

import pandas as pd

from ..config import AlgoConfig
from .table_input_sql import SqlTableInput, quote_literal, quote_literal_list

#: PG 系的系统 schema，探查时一律排除。**方言规则，不是用户配置** ——
#: 它只对 PG 成立，没有理由让 MySQL / ODPS 也在 AlgoConfig 里看见它。
PG_SYSTEM_SCHEMAS: tuple[str, ...] = (
    "pg_catalog",
    "information_schema",
    "hologres",
    "pg_toast",
    "hg_recyclebin",
    "hologres_statistic",
)

#: ``object_type`` / ``view_definition`` 是额外列（同 ``table_schema`` /
#: ``ordinal_position``）：契约不要求，但 dataset 直通要用——直通对象的
#: ``dataset_type`` 直接取 relkind 的可读形式，``sql_content`` 直接取视图定义，
#: 都是事实而不是从表名猜。物化视图不在 ``pg_views`` 里，``view_definition``
#: 因此为 NULL，这是对的：它的定义要另查 ``pg_matviews``，而直通并不需要。
_SQL_TABLES = """
    SELECT n.nspname AS table_schema,
           c.relname AS table_name,
           d.description AS table_comment,
           CASE c.relkind
               WHEN 'r' THEN 'TABLE'
               WHEN 'v' THEN 'VIEW'
               WHEN 'm' THEN 'MATERIALIZED_VIEW'
           END AS object_type,
           v.definition AS view_definition
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
    LEFT JOIN pg_views v ON v.viewname = c.relname AND v.schemaname = n.nspname
    WHERE c.relkind IN ('r', 'v', 'm')          -- 普通表 / 视图 / 物化视图
      AND n.nspname NOT IN ({system_schemas})
      {exclude_tables}
    ORDER BY n.nspname, c.relname
"""

#: **SQL 直接产出契约要求的列名与取值**，不靠事后改名。
#: 我们自己写的 SQL，没有理由拉完 is_primary_key 再 rename 成 is_primary ——
#: 那层别名表正是这么冒出来的，而它曾经漏掉映射、让主键信息全部丢失。
#:
#: ordinal_position 是额外列：契约不要求，但真实有用，因此保留而不丢弃。
_SQL_COLUMNS = """
    SELECT a.attname AS column_name,
           CASE WHEN a.attndims > 0 THEN t.typname || '[]' ELSE t.typname END AS data_type,
           CASE WHEN a.attnotnull THEN 'N' ELSE 'Y' END AS is_nullable,
           d.description AS column_comment,
           NULL::text AS column_name_cn,
           CASE WHEN con.conname IS NOT NULL THEN 'Y' ELSE 'N' END AS is_primary,
           a.attnum AS ordinal_position
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_type t ON t.oid = a.atttypid
    LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
    LEFT JOIN pg_constraint con ON con.conrelid = c.oid
                               AND con.contype = 'p'
                               AND a.attnum = ANY(con.conkey)
    WHERE a.attrelid = {table_literal}::regclass
      AND a.attnum > 0                          -- 排除系统隐藏列
      AND NOT a.attisdropped
    ORDER BY a.attnum
"""


class PGSourceTableInput(SqlTableInput):
    """PostgreSQL / Hologres 物理表。

    Date-suffixed tables share a logical name, but column and enum queries
    use the latest physical member, qualified by its schema.

    ``table_schema`` / ``ordinal_position`` / ``object_type`` /
    ``view_definition`` 作为额外列保留 —— 契约不要求，但它们是真实信息，
    没有理由在入口丢掉。后两个是 dataset 直通的依据。
    """

    dialect_keys = ("postgresql", "pg")

    dialect = "postgres"

    def __init__(self, config: AlgoConfig) -> None:
        super().__init__(config)
        self._table_references: dict[str, str] = {}

    async def fetch_table_meta(self) -> pd.DataFrame:
        frame = await self.sql(self.sql_tables())
        self._table_references = {}
        if frame.empty:
            return frame
        frame = frame.copy()
        frame["table_fullname"] = frame["table_schema"] + "." + frame["table_name"]
        references = {
            row.table_fullname: (
                f"{self.quote_ident(row.table_schema)}.{self.quote_ident(row.table_name)}"
            )
            for row in frame.itertuples(index=False)
        }
        if self.config.input_table_merge_partitions_enabled:
            # sql_tables orders physical names so keep="last" selects the latest member.
            frame = self.merge_partitioned(frame)
        self._table_references = {
            row.table_name: references[row.table_fullname]
            for row in frame.itertuples(index=False)
        }
        return frame

    def sql_tables(self) -> str:
        """系统 schema 由 :data:`PG_SYSTEM_SCHEMAS` 直接排除（方言规则）；
        表名排除模式来自 ``config.input_table_exclude_patterns``（用户配置）。
        """
        exclude_tables = "\n      ".join(
            f"AND c.relname NOT LIKE {quote_literal(p)}"
            for p in self.config.input_table_exclude_patterns
        )
        return _SQL_TABLES.format(
            system_schemas=quote_literal_list(PG_SYSTEM_SCHEMAS),
            exclude_tables=exclude_tables,
        )

    def sql_columns(self, table_name: str) -> str:
        return _SQL_COLUMNS.format(
            table_literal=quote_literal(self._table_references[table_name])
        )

    def sql_distinct(
        self,
        table_name: str,
        column_name: str,
        partition: str | None,
    ) -> str:
        source = self._table_references[table_name]
        where = ""
        if partition:
            part = self.quote_ident(partition)
            # 只探最新分区，避免全表扫描
            where = f" WHERE {part} = (SELECT MAX({part}) FROM {source})"
        return (
            f"SELECT DISTINCT {self.quote_ident(column_name)} AS v "
            f"FROM {source}{where} LIMIT {self.config.input_table_enums_max_distinct}"
        )

    def quote_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'


__all__ = ["PG_SYSTEM_SCHEMAS", "PGSourceTableInput"]
