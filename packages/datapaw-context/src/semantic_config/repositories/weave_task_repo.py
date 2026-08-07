from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT w.id, w.task_id, w.task_name, w.datasource_id, s.datasource_name,
           w.weave_mode, w.status, w.error_msg, w.created_at
    FROM weave_task w
    LEFT JOIN datasource s ON s.datasource_id = w.datasource_id AND s.is_deleted = 0
"""


async def insert(db, task_id, task_name, datasource_id, weave_mode, status, export_payload, error_msg) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO weave_task
            (task_id, task_name, datasource_id, weave_mode, status, export_payload, error_msg,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        task_id, task_name, datasource_id, weave_mode, status, export_payload, error_msg, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def find_by_task_id(db: aiosqlite.Connection, task_id: str) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM weave_task WHERE task_id = ? AND is_deleted = 0", (task_id,)
    ) as cur:
        return await cur.fetchone()


async def find_response_by_task_id(db: aiosqlite.Connection, task_id: str) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE w.is_deleted = 0 AND w.task_id = ?", (task_id,)) as cur:
        return await cur.fetchone()


async def update_status(db: aiosqlite.Connection, task_id: str, status: str, error_msg: str | None) -> int:
    cur = await db.execute(
        "UPDATE weave_task SET status = ?, error_msg = ?, updated_at = ? WHERE task_id = ? AND is_deleted = 0",
        (status, error_msg, now_iso(), task_id),
    )
    return cur.rowcount


def _build_where(datasource_name: str | None, task_name: str | None, params: list) -> str:
    where = ["w.is_deleted = 0"]
    if datasource_name:
        where.append("s.datasource_name LIKE ?"); params.append(f"%{datasource_name}%")
    if task_name:
        where.append("w.task_name LIKE ?"); params.append(f"%{task_name}%")
    return " AND ".join(where)


async def count(db, datasource_name, task_name) -> int:
    params: list = []
    where = _build_where(datasource_name, task_name, params)
    sql = f"""
        SELECT COUNT(1) AS c FROM weave_task w
        LEFT JOIN datasource s ON s.datasource_id = w.datasource_id AND s.is_deleted = 0
        WHERE {where}
    """
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_name, task_name, limit, off):
    params: list = []
    where = _build_where(datasource_name, task_name, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY w.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()
