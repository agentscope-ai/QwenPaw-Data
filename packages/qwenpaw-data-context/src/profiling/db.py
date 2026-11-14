"""profiling 子系统的 DB 辅助（异构存储：SQLite / Hologres）。

与 semantic_config 共用同一 SQLAlchemy 引擎（`semantic_config.engine`）与物理库：
- 请求作用域会话、时间戳复用 `semantic_config.db.get_db` / `now_iso`（不重复实现）。
- 本模块负责 profiling 自己的建表及旧表升级（`init_probe_db`）与后台任务用的独立会话
  （`open_probe_conn`，供异步探查任务脱离请求上下文时自开会话，见 SPEC §6.1）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.engine import (
    get_engine,
    get_session_factory,
    lock_schema_initialization,
)


async def init_probe_db() -> None:
    """建 profiling 的 9 张表并升级旧 staging 字段（幂等，不重置引擎）。"""
    from profiling.schema_migration import migrate_legacy_probe_schema
    from profiling.tables import PROFILING_TABLE_LIST  # noqa: F401 触发表在共享 metadata 上注册
    from semantic_config.tables import metadata

    engine = get_engine()
    async with engine.begin() as conn:
        await lock_schema_initialization(conn)
        await conn.run_sync(
            lambda c: metadata.create_all(c, tables=PROFILING_TABLE_LIST)
        )
    await migrate_legacy_probe_schema(engine)


async def open_probe_conn() -> AsyncSession:
    """为后台探查任务打开一个独立 AsyncSession（调用方负责 commit/close）。

    后台任务不能复用请求作用域的 `get_db`（其会话随请求结束关闭）。见 SPEC §6.1。
    """
    return get_session_factory()()
