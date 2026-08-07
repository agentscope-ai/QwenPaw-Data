from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso


async def insert(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    config_json: str | None = None,
) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO datasource (datasource_id, datasource_name, datasource_type, config, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (datasource_id, name, dtype, config_json, ts, ts)) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(
    db: aiosqlite.Connection,
    ds_id: int,
    *,
    name: str | None = None,
    dtype: str | None = None,
    config_json: str | None = None,
    update_config: bool = False,
) -> int:
    """按字段更新数据源。仅更新非 None 的 name/dtype；config 仅在 update_config=True 时替换。"""
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("datasource_name = ?")
        params.append(name)
    if dtype is not None:
        sets.append("datasource_type = ?")
        params.append(dtype)
    if update_config:
        sets.append("config = ?")
        params.append(config_json)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(ds_id)
    sql = f"UPDATE datasource SET {', '.join(sets)} WHERE id = ? AND is_deleted = 0"
    cur = await db.execute(sql, params)
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, ds_id: int) -> aiosqlite.Row | None:
    sql = "SELECT * FROM datasource WHERE id = ? AND is_deleted = 0"
    async with db.execute(sql, (ds_id,)) as cur:
        return await cur.fetchone()


async def find_by_datasource_id(
    db: aiosqlite.Connection, datasource_id: str
) -> aiosqlite.Row | None:
    sql = "SELECT * FROM datasource WHERE datasource_id = ? AND is_deleted = 0"
    async with db.execute(sql, (datasource_id,)) as cur:
        return await cur.fetchone()


def _build_where(datasource_id: str | None, name: str | None, dtype: str | None, params: list) -> str:
    where = ["is_deleted = 0"]
    if datasource_id:
        where.append("datasource_id LIKE ?")
        params.append(f"%{datasource_id}%")
    if name:
        where.append("datasource_name LIKE ?")
        params.append(f"%{name}%")
    if dtype:
        where.append("datasource_type = ?")
        params.append(dtype)
    return " AND ".join(where)


async def count(db: aiosqlite.Connection, datasource_id: str | None, name: str | None, dtype: str | None) -> int:
    params: list = []
    where = _build_where(datasource_id, name, dtype, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM datasource WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    limit: int,
    off: int,
) -> list[aiosqlite.Row]:
    params: list = []
    where = _build_where(datasource_id, name, dtype, params)
    sql = f"SELECT * FROM datasource WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, ds_id: int) -> int:
    sql = "UPDATE datasource SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0"
    cur = await db.execute(sql, (now_iso(), ds_id))
    return cur.rowcount


# ---- 引用计数（删除校验用）：按数据源对外编码统计业务域 ----
async def count_domains(db: aiosqlite.Connection, datasource_id: str) -> int:
    sql = "SELECT COUNT(1) AS c FROM biz_domain WHERE datasource_id = ? AND is_deleted = 0"
    async with db.execute(sql, (datasource_id,)) as cur:
        row = await cur.fetchone()
    return int(row["c"])
