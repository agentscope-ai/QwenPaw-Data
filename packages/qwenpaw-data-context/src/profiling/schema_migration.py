"""升级旧 staging 字段；只处理已知变更，不重建表或重写候选记录。"""

from __future__ import annotations

import logging

from sqlalchemy import DDL, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from semantic_config.engine import lock_schema_initialization

log = logging.getLogger(__name__)

# 表名、列名均为内部常量，不接受请求输入。
_ADDED_COLUMNS = {
    "staging_dimension": ("dimension_type", "child_dimensions", "priority"),
    "staging_metric": ("priority",),
}


def _columns(conn: Connection, table: str) -> dict[str, str | None]:
    if conn.dialect.name == "sqlite":
        return {
            row[1]: row[4]
            for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')
        }
    # 按真实 search_path 解析表；不能把多个 schema 的同名表合并，
    # 也不能假设应用总是连接 public。结果形状不随 ALTER 改变。
    rows = conn.execute(
        text("""
        SELECT column_name, column_default
          FROM information_schema.columns
         WHERE table_name = :table_name
           AND table_schema = (
               SELECT n.nspname
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.oid = to_regclass(:table_name)
           )
    """),
        {"table_name": table},
    )
    return {row[0]: row[1] for row in rows}


def _add_or_rename(conn: Connection, sql: str, table: str, column: str) -> None:
    try:
        # DDL 对象会使 SQLAlchemy 的 asyncpg schema/statement cache 失效。
        conn.execute(DDL(sql))
    except DBAPIError as exc:
        # 多个服务进程可能同时升级。只接受明确的列已存在/已改名竞争；
        # 权限、连接和其他 DDL 错误必须继续向启动调用方报告。
        if conn.dialect.name != "postgresql" or getattr(
            exc.orig, "sqlstate", None
        ) not in {
            "42701",
            "42703",
        }:
            raise
        conn.rollback()
        if column not in _columns(conn, table):
            raise
    else:
        log.info("profiling schema upgraded: %s.%s", table, column)


def _migrate(conn: Connection) -> None:
    for table, additions in _ADDED_COLUMNS.items():
        columns = _columns(conn, table)
        if not columns:
            raise RuntimeError(f"profiling schema migration cannot find table {table}")
        for column in additions:
            if column in columns:
                continue
            if column == "dimension_type" and "dimension_role" in columns:
                # 就地改名保留所有租户、审核状态和软删记录的原值。
                # 两列已并存时以上面的新列为准，不覆盖新值或显式 NULL。
                sql = f'ALTER TABLE "{table}" RENAME COLUMN "dimension_role" TO "{column}"'
            else:
                sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT'
                if conn.dialect.name == "sqlite" and column == "priority":
                    sql += " DEFAULT 'MEDIUM'"
            _add_or_rename(conn, sql, table, column)

        if conn.dialect.name == "postgresql":
            # Hologres 用独立 SET DEFAULT；不要依赖 ADD COLUMN DEFAULT / IF NOT EXISTS。
            # 每次检查可恢复 ADD 已提交、SET DEFAULT 尚未完成的中断升级。
            # 旧行仍可为 NULL，promote._payload 已按 MEDIUM 处理；不做 DML 回填。
            if _columns(conn, table)["priority"] is None:
                conn.execute(
                    DDL(
                        f'ALTER TABLE "{table}" ALTER COLUMN "priority" SET DEFAULT \'MEDIUM\''
                    )
                )


async def migrate_legacy_probe_schema(engine: AsyncEngine) -> None:
    """在 create_all 提交后升级；重复调用不覆盖候选值。"""
    if engine.dialect.name == "postgresql" and "hologres.aliyuncs.com" not in (
        engine.url.host or ""
    ):
        async with engine.begin() as conn:
            await lock_schema_initialization(conn)
            await conn.run_sync(_migrate)
        return
    async with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            # Hologres 不支持 DDL/DML 混合事务及 SAVEPOINT。逐条提交 DDL，
            # 任一步失败后下次启动按实际结构继续，不需要删表或回放业务写入。
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        else:
            # SQLite 无 ADD COLUMN IF NOT EXISTS，在内省前取得写锁防止并发补列。
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        await conn.run_sync(_migrate)
        await conn.commit()
