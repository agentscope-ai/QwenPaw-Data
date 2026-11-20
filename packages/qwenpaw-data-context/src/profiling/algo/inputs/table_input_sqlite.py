"""SQLite 的物理表来源。

只提供 SQL 文本与引用规则；并发、预算、分区识别在
:class:`~.table_input_sql.SqlTableInput`。

SQLite 相对 PG 有三个「没有」，都在本文件显式表达，不留给下游猜：

- **没有注释**：DDL 不支持 COMMENT，``table_comment`` / ``column_comment``
  恒为 NULL。
- **没有 schema**：表名全局唯一，系统表以 ``sqlite_`` 前缀区分
  （方言规则，对应 PG 的 ``PG_SYSTEM_SCHEMAS``）。
- **没有分区**：:meth:`~SqliteSourceTableInput.merge_partitioned` 覆写为
  原样返回——合并出来的基表名在库里并不存在，后续
  ``pragma_table_info`` / ``SELECT DISTINCT`` 会整体扑空。

首个使用场景是 spider2-lite 的本地库。执行器工厂
（``build_sqlite_execute_sql``）住 ``profiling.algo.sql`` —— 本文件只管
SQL 文本长什么样。要求 SQLite ≥ 3.16（2017，表值 pragma 函数）。
"""

from __future__ import annotations

import pandas as pd

from .table_input_sql import quote_literal
from .table_input_sql import SqlTableInput

#: SQLite 内部表前缀（sqlite_master / sqlite_sequence / sqlite_stat1…）。
#: 引擎保留该前缀，用户建不出同前缀的表，按前缀排除即完备。
SQLITE_SYSTEM_TABLE_PREFIX: str = "sqlite_"

#: ``object_type`` / ``view_definition`` 与 PG 版对齐，供 dataset 直通。
#: SQLite 只存 DDL 原文，因此 ``view_definition`` 是完整的
#: ``CREATE VIEW … AS SELECT …``，而不是像 PG 那样只有 SELECT 体。
_SQL_TABLES = """
    SELECT m.name AS table_name,
           NULL AS table_comment,
           CASE m.type WHEN 'table' THEN 'TABLE' WHEN 'view' THEN 'VIEW' END AS object_type,
           CASE WHEN m.type = 'view' THEN m.sql END AS view_definition
    FROM sqlite_master AS m
    WHERE m.type IN ('table', 'view')
      AND m.name NOT LIKE '{system_prefix}%' ESCAPE '\\'
      {exclude_tables}
    ORDER BY m.name
"""

#: **SQL 直接产出契约要求的列名与取值**，不靠事后改名（同 PG 版）。
#:
#: ``NULLIF(i.type, '')``：SQLite 允许无类型列（视图列全是），空串是
#: 「没有声明类型」而不是一个类型名，按 NULL 交出。
#: ``pk > 0``：复合主键时 pk 是序号（1、2、…），非 0 即主键成员。
_SQL_COLUMNS = """
    SELECT i.name AS column_name,
           NULLIF(i.type, '') AS data_type,
           CASE WHEN i."notnull" = 1 THEN 'N' ELSE 'Y' END AS is_nullable,
           NULL AS column_comment,
           NULL AS column_name_cn,
           CASE WHEN i.pk > 0 THEN 'Y' ELSE 'N' END AS is_primary,
           i.cid + 1 AS ordinal_position
    FROM pragma_table_info({table_literal}) AS i
    ORDER BY i.cid
"""


class SqliteSourceTableInput(SqlTableInput):
    """SQLite 物理表 + 视图。

    ``object_type`` / ``view_definition`` / ``ordinal_position`` 作为额外列
    保留，语义与 PG 版一致。表不存在时 ``pragma_table_info`` 返回空集而非
    报错，正好落进「缺失的表可以不出现在返回值里」的约定。
    """

    dialect_keys = ("sqlite",)
    dialect = "sqlite"

    def sql_tables(self) -> str:
        """系统表按 ``sqlite_`` 前缀排除（方言规则）；
        表名排除模式来自 ``config.input_table_exclude_patterns``（用户配置）。
        """
        exclude_tables = "\n      ".join(
            f"AND m.name NOT LIKE {quote_literal(p)}"
            for p in self.config.input_table_exclude_patterns
        )
        return _SQL_TABLES.format(
            system_prefix=SQLITE_SYSTEM_TABLE_PREFIX.replace("_", "\\_"),
            exclude_tables=exclude_tables,
        )

    def sql_columns(self, table_name: str) -> str:
        return _SQL_COLUMNS.format(table_literal=quote_literal(table_name))

    def sql_distinct(
        self,
        table_name: str,
        column_name: str,
        partition: str | None,
    ) -> str:
        # 表名必须引用：spider2 的 IPL 库就有一张叫 match 的表（SQLite 关键字）。
        # PG 版不引是因为那边的表名可能带 schema 限定；这里没有 schema。
        source = self.quote_ident(table_name)
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

    def merge_partitioned(self, frame: pd.DataFrame) -> pd.DataFrame:
        """原样返回。SQLite 没有分区表；``xxx_20260101`` 若真出现也是一张
        独立物理表，归并出的基表名无法查询。
        """
        return frame


__all__ = [
    "SQLITE_SYSTEM_TABLE_PREFIX",
    "SqliteSourceTableInput",
]
