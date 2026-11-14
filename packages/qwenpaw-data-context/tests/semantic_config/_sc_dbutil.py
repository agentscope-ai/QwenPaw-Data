"""测试辅助：构造 semantic_config / profiling 的 SQLAlchemy 会话。

异构存储改造后 repo/service 接收 AsyncSession（而非 aiosqlite.Connection）。这里提供：
- `new_memory_session`：独立 in-memory SQLite（StaticPool 保证单连接内建表可见），服务层直调用。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


class _MemorySession(AsyncSession):
    """Close the single-session test database before its event loop exits."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine, expire_on_commit=False)
        self._owned_engine = engine

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self._owned_engine.dispose()


async def new_memory_session() -> AsyncSession:
    import profiling.tables  # noqa: F401 触发 profiling 8 表在共享 metadata 上注册
    from semantic_config.tables import metadata

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return _MemorySession(engine)


async def seed_physical_table(
    db,
    datasource_id: str,
    table_name: str,
    tenant_id: str,
    workspace_id: str,
    *,
    created_by: str = "alice",
) -> int:
    """预建一行 physical_table，返回其 id。

    table_column.table_id NOT NULL 且外键指向 physical_table.id：插入列前物理表须先声明。
    """
    from sqlalchemy import insert as sa_insert

    from semantic_config.db import now_iso
    from semantic_config.tables import physical_table

    ts = now_iso()
    res = await db.execute(
        sa_insert(physical_table).values(
            datasource_id=datasource_id,
            table_name=table_name,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            updated_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
    )
    return int(res.inserted_primary_key[0])


async def new_memory_factory():
    """多会话共享同一 in-memory 库（mg-088：后台 worker 自建会话场景）。

    返回 ``(session_factory, engine)``；不同会话经 StaticPool 命中同一物理库。
    """
    import profiling.tables  # noqa: F401
    from semantic_config.tables import metadata

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine
