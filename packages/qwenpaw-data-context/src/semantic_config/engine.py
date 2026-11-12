"""异步 SQLAlchemy 引擎（仿 host `db/engine.py`）：单例 Engine + AsyncSession 工厂。

- URL 来源：统一环境变量 `QWENPAW_DATA_DB_URL`（与 host / profiling 复用同一变量、同一物理库）；
  未设置时使用 `QWENPAW_DATA_HOME` 下与 Auth 共享的本地 SQLite 库。
- 驱动归一：无论共享 URL 写成何种驱动后缀，本模块一律归一为异步驱动
  （SQLite→`sqlite+aiosqlite`、Hologres/PG→`postgresql+asyncpg`），与 host 异步栈一致。
- SQLite 分支：建目录 + `PRAGMA journal_mode=WAL`/`foreign_keys=OFF`（connect 事件）；
  Hologres 分支跳过。
- Hologres/PG 分支：`pool_pre_ping` + `pool_recycle` 兜住长时间空闲后被中间设备
  掐断的池内连接（SQLite 无此问题，不设）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qwenpaw_data_auth.database import shared_database_url
from semantic_config.config import get_settings

_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None
_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)

# 池中连接可能空闲数十分钟（后台织入把长图导入排在两段短事务之间，期间不持有会话），
# 这段时间里 SLB / 防火墙的 TCP 空闲回收会静默掐断连接。pre_ping 在借出时探活并
# 透明重连；recycle 主动丢弃超龄连接——取值须小于基础设施的空闲回收阈值。
_PG_POOL_RECYCLE_SECONDS = 600


async def lock_schema_initialization(connection) -> None:
    """Serialize PostgreSQL schema inspection and DDL across service processes."""
    if connection.dialect.name == "postgresql" and "hologres.aliyuncs.com" not in (
        connection.engine.url.host or ""
    ):
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": 0x53435052},
        )


def _resolve_url() -> str:
    """把配置解析为异步 SQLAlchemy URL（driver 归一）。"""
    url = make_url(shared_database_url(get_settings().db_url or ""))

    if url.get_backend_name() == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    else:
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


def get_engine() -> AsyncEngine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    url = _resolve_url()
    sa_url = make_url(url)
    if sa_url.get_backend_name() == "sqlite":
        database = sa_url.database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine_kwargs: dict = {}
    if sa_url.get_backend_name() == "sqlite":
        engine_kwargs["connect_args"] = {"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}
    else:
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = _PG_POOL_RECYCLE_SECONDS
    engine = create_async_engine(url, **engine_kwargs)

    if sa_url.get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            # journal_mode 是文件持久属性；foreign_keys 会话级（本 schema 无 DB 外键）。
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

    _engine = engine
    _SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def reset_engine() -> None:
    """释放并清空单例（供 init_db 换库、测试切换 QWENPAW_DATA_DB_URL 使用）。"""
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _SessionLocal = None
