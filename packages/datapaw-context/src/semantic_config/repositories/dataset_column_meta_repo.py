from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT c.id, c.dataset_id, ds.dataset_name,
           c.datasource_id, c.domain_id, s.datasource_name, b.domain_name,
           c.column_name, c.column_name_cn, c.data_type, c.column_type, c.dimension_type,
           c.column_comment, c.column_enums, c.column_enums_description, c.samples,
           c.is_primary, c.is_nullable
    FROM dataset_column_meta c
    LEFT JOIN dataset_meta ds ON ds.id = c.dataset_id AND ds.is_deleted = 0
    LEFT JOIN datasource s ON s.datasource_id = c.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = c.domain_id AND b.is_deleted = 0
"""


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO dataset_column_meta
            (dataset_id, datasource_id, domain_id, column_name, column_name_cn, data_type,
             column_type, dimension_type, column_comment, column_enums, column_enums_description,
             samples, is_primary, is_nullable, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.dataset_id, m.datasource_id, m.domain_id, m.column_name, m.column_name_cn, m.data_type,
        m.column_type, m.dimension_type, m.column_comment, m.column_enums, m.column_enums_description,
        m.samples, m.is_primary or "N", m.is_nullable or "Y", ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, col_id: int, m) -> int:
    sql = """
        UPDATE dataset_column_meta SET
            column_name = ?, column_name_cn = ?, data_type = ?, column_type = ?, dimension_type = ?,
            column_comment = ?, column_enums = ?, column_enums_description = ?, samples = ?,
            is_primary = ?, is_nullable = ?, updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (
        m.column_name, m.column_name_cn, m.data_type, m.column_type, m.dimension_type,
        m.column_comment, m.column_enums, m.column_enums_description, m.samples,
        m.is_primary, m.is_nullable, now_iso(), col_id,
    ))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, col_id: int) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM dataset_column_meta WHERE id = ? AND is_deleted = 0", (col_id,)
    ) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, col_id: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE c.is_deleted = 0 AND c.id = ?", (col_id,)) as cur:
        return await cur.fetchone()


async def exists_name_in_dataset(db: aiosqlite.Connection, dataset_id: int, column_name: str) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_column_meta "
        "WHERE dataset_id = ? AND column_name = ? AND is_deleted = 0",
        (dataset_id, column_name),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


async def exists_name_in_dataset_excluding(
    db: aiosqlite.Connection, dataset_id: int, column_name: str, col_id: int
) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_column_meta "
        "WHERE dataset_id = ? AND column_name = ? AND id <> ? AND is_deleted = 0",
        (dataset_id, column_name, col_id),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


def _build_where(datasource_id, domain_id, dataset_id, params: list) -> str:
    where = ["c.is_deleted = 0"]
    if datasource_id is not None:
        where.append("c.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("c.domain_id = ?"); params.append(domain_id)
    if dataset_id is not None:
        where.append("c.dataset_id = ?"); params.append(dataset_id)
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, dataset_id) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_id, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM dataset_column_meta c WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, dataset_id, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_id, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY c.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def find_all_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> list[aiosqlite.Row]:
    sql = _RESPONSE_SELECT + " WHERE c.is_deleted = 0 AND c.dataset_id = ? ORDER BY c.id"
    async with db.execute(sql, (dataset_id,)) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, col_id: int) -> int:
    cur = await db.execute(
        "UPDATE dataset_column_meta SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), col_id),
    )
    return cur.rowcount
