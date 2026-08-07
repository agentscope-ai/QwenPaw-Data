from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from semantic_config.config import get_settings


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


async def init_db() -> None:
    """启动时执行 schema.sql 建表（幂等）。"""
    settings = get_settings()
    schema = Path(settings.schema_path)
    if not schema.exists():
        # 回退到包内 schema.sql（合并后从 CM 根目录启动时更稳妥）
        schema = Path(__file__).resolve().parent / "schema.sql"
    ddl = schema.read_text(encoding="utf-8")
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.executescript(ddl)
        await conn.commit()


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI 依赖：每请求一个连接、一个事务。

    正常结束 -> commit；抛异常 -> rollback（整请求原子，Excel 导入据此实现整体回滚）。
    """
    settings = get_settings()
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()
