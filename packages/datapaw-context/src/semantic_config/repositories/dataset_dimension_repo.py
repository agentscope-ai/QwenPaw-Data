from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT dd.id, dd.dataset_id, ds.dataset_name,
           dd.dimension_id, dim.dimension_name,
           dd.datasource_id, dd.domain_id, s.datasource_name, b.domain_name,
           dd.calculate_expr, dd.dimension_type, dd.data_type
    FROM dataset_dimension dd
    LEFT JOIN dataset_meta ds ON ds.id = dd.dataset_id AND ds.is_deleted = 0
    LEFT JOIN dimension dim ON dim.id = dd.dimension_id AND dim.is_deleted = 0
    LEFT JOIN datasource s ON s.datasource_id = dd.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = dd.domain_id AND b.is_deleted = 0
"""


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO dataset_dimension
            (dataset_id, dimension_id, datasource_id, domain_id, calculate_expr,
             dimension_type, data_type, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.dataset_id, m.dimension_id, m.datasource_id, m.domain_id, m.calculate_expr,
        m.dimension_type, m.data_type, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, dd_id: int, m) -> int:
    sql = """
        UPDATE dataset_dimension SET
            calculate_expr = ?, dimension_type = ?, data_type = ?, updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (m.calculate_expr, m.dimension_type, m.data_type, now_iso(), dd_id))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, dd_id: int) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM dataset_dimension WHERE id = ? AND is_deleted = 0", (dd_id,)
    ) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, dd_id: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE dd.is_deleted = 0 AND dd.id = ?", (dd_id,)) as cur:
        return await cur.fetchone()


def _build_where(datasource_id, domain_id, dataset_id, dataset_name, dimension_name, params: list) -> str:
    where = ["dd.is_deleted = 0"]
    if datasource_id is not None:
        where.append("dd.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("dd.domain_id = ?"); params.append(domain_id)
    if dataset_id is not None:
        where.append("dd.dataset_id = ?"); params.append(dataset_id)
    if dataset_name:
        where.append("ds.dataset_name LIKE ?"); params.append(f"%{dataset_name}%")
    if dimension_name:
        where.append("dim.dimension_name LIKE ?"); params.append(f"%{dimension_name}%")
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_id, dataset_name, dimension_name, params)
    sql = f"""
        SELECT COUNT(1) AS c FROM dataset_dimension dd
        LEFT JOIN dataset_meta ds ON ds.id = dd.dataset_id AND ds.is_deleted = 0
        LEFT JOIN dimension dim ON dim.id = dd.dimension_id AND dim.is_deleted = 0
        WHERE {where}
    """
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_id, dataset_name, dimension_name, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY dd.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def find_all_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> list[aiosqlite.Row]:
    sql = _RESPONSE_SELECT + " WHERE dd.is_deleted = 0 AND dd.dataset_id = ? ORDER BY dd.id"
    async with db.execute(sql, (dataset_id,)) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, dd_id: int) -> int:
    cur = await db.execute(
        "UPDATE dataset_dimension SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), dd_id),
    )
    return cur.rowcount


async def soft_delete_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> int:
    cur = await db.execute(
        "UPDATE dataset_dimension SET is_deleted = 1, updated_at = ? WHERE dataset_id = ? AND is_deleted = 0",
        (now_iso(), dataset_id),
    )
    return cur.rowcount
