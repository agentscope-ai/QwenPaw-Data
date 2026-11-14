"""semantic_config 数据访问入口（异构存储：SQLite / Hologres）。

自 SPEC 03 起，底层由 `aiosqlite` 手写 SQL 切换为 SQLAlchemy Core + 异步引擎
（见 `engine.py` 与 `tables.py`）。本模块只保留对外契约：

- `get_db`：FastAPI 依赖，每请求一个 `AsyncSession` / 一个事务（commit-before-response 语义保持不变）。
- `now_iso`：写入 created_at / updated_at 的时间戳。
- `init_db`：启动建表（幂等），SQLite 分支附带旧库 datasource_id 迁移。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.config import get_settings
from semantic_config.engine import (
    get_engine,
    get_session_factory,
    lock_schema_initialization,
    reset_engine,
)

_DATASOURCE_CHILD_TABLES = (
    # 12 实体重构后仍直挂 datasource_id 的正式表；domain / dimension_dict /
    # metric_dict 已与数据源解耦、无该列，故不登记（登记也会被下面的列探测跳过）。
    "dataset",
    "biz_domain",
    "dataset_meta",
    "dataset_column_meta",
    "dimension",
    "dataset_dimension",
    "dataset_dimension_value",
    "metric_lib",
    "metric_formula_lib",
    "weave_task",
    # mg-088：新表自始即字符串 datasource_id，迁移 SQL 按 typeof 过滤天然空转，
    # 登记只为初始化/迁移表清单完备（spec §5.4 适配点）。
    "explore_scope",
    "explore_state",
)


def _tz() -> timezone:
    """把 settings.tz_offset（如 +08:00）解析成 timezone。"""
    raw = get_settings().tz_offset.strip()
    if not raw or raw.upper() == "Z":
        return timezone.utc
    sign = 1 if raw[0] != "-" else -1
    hh, mm = raw.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def now_iso() -> str:
    """当前时间，ISO8601 带偏移，供 created_at / updated_at 写入。"""
    return datetime.now(_tz()).isoformat(timespec="seconds")


_LEGACY_MIGRATE_SQL = """
UPDATE {table}
   SET datasource_id = (
       SELECT d.datasource_id
         FROM datasource AS d
        WHERE d.id = CAST({table}.datasource_id AS INTEGER)
          AND d.is_deleted = 0
   )
 WHERE typeof(datasource_id) IN ('integer', 'real')
   AND EXISTS (
       SELECT 1
         FROM datasource AS d
        WHERE d.id = CAST({table}.datasource_id AS INTEGER)
          AND d.is_deleted = 0
   )
"""


def _legacy_table_has_datasource_id_sync(conn, table: str) -> bool:  # noqa: ANN001
    """init_db 内 run_sync 调用的同步探测版本（仅 SQLite）。"""
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return False
    cols = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == "datasource_id" for c in cols)


def _migrate_legacy_datasource_ids_sync(conn) -> None:  # noqa: ANN001
    """init_db 内经 run_sync 调用的同步版本（仅 SQLite）。"""
    for table in _DATASOURCE_CHILD_TABLES:
        if not _legacy_table_has_datasource_id_sync(conn, table):
            continue
        conn.exec_driver_sql(_LEGACY_MIGRATE_SQL.format(table=table))


def _ensure_datasource_description_sync(conn) -> None:  # noqa: ANN001
    """兼容已创建但尚无 description 列的 SQLite datasource 表。"""
    columns = conn.exec_driver_sql("PRAGMA table_info(datasource)").fetchall()
    if columns and "description" not in {str(column[1]) for column in columns}:
        conn.exec_driver_sql("ALTER TABLE datasource ADD COLUMN description TEXT")


def _ensure_biz_domain_v21_sync(conn) -> None:  # noqa: ANN001
    """兼容尚无 v2.1 列的 SQLite biz_domain 表（display_name；datasource_scope 暂缓）。"""
    columns = conn.exec_driver_sql("PRAGMA table_info(biz_domain)").fetchall()
    if not columns:
        return
    existing = {str(column[1]) for column in columns}
    if "display_name" not in existing:
        conn.exec_driver_sql("ALTER TABLE biz_domain ADD COLUMN display_name TEXT")
    # TODO(v2.1-followup): datasource_scope 暂缓启用，见 template_contract._DOMAIN。
    # if "datasource_scope" not in existing:
    #     conn.exec_driver_sql("ALTER TABLE biz_domain ADD COLUMN datasource_scope TEXT")


def _ensure_dataset_meta_v21_sync(conn) -> None:  # noqa: ANN001
    """兼容尚无归属列的 SQLite dataset_meta 表（datasource_id）。"""
    columns = conn.exec_driver_sql("PRAGMA table_info(dataset_meta)").fetchall()
    if not columns:
        return
    if "datasource_id" not in {str(column[1]) for column in columns}:
        conn.exec_driver_sql("ALTER TABLE dataset_meta ADD COLUMN datasource_id TEXT")


def _ensure_stateless_columns(conn) -> None:
    from context_manager.migration.stateless import ensure_deployed_stateless_columns

    ensure_deployed_stateless_columns(conn)


async def init_db() -> None:
    """启动建表（幂等）。SQLite 分支附带旧库 datasource_id 迁移。

    重建引擎以吸收 QWENPAW_DATA_DB_URL 的运行期变化（测试切库、初始化场景）。
    """
    from semantic_config.tables import SC_TABLE_LIST, metadata

    await reset_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await lock_schema_initialization(conn)
        await conn.run_sync(lambda c: metadata.create_all(c, tables=SC_TABLE_LIST))
        await conn.run_sync(_ensure_stateless_columns)
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_ensure_datasource_description_sync)
            await conn.run_sync(_ensure_biz_domain_v21_sync)
            await conn.run_sync(_ensure_dataset_meta_v21_sync)
            await conn.run_sync(_migrate_legacy_datasource_ids_sync)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 AsyncSession、一个事务。

    正常结束 -> commit；抛异常 -> rollback（整请求原子，Excel 导入据此实现整体回滚）。
    路由必须声明 ``Depends(get_db, scope="function")``，使这里的提交在响应发出前完成。
    """
    del request
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
