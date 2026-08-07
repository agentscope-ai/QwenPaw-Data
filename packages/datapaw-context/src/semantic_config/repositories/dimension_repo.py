from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT d.id, d.datasource_id, d.domain_id,
           s.datasource_name, b.domain_name,
           d.dimension_name, d.description, d.parent_name, d.depth, d.synonyms,
           d.is_visible, d.is_attribution, d.enums
    FROM dimension d
    LEFT JOIN datasource s ON s.datasource_id = d.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = d.domain_id AND b.is_deleted = 0
"""


def _b(v):
    return None if v is None else (1 if v else 0)


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO dimension
            (datasource_id, domain_id, dimension_name, description, parent_name, depth,
             synonyms, is_visible, is_attribution, enums, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.datasource_id, m.domain_id, m.dimension_name, m.description, m.parent_name, m.depth,
        m.synonyms, _b(m.is_visible), _b(m.is_attribution), m.enums, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, dim_id: int, m) -> int:
    sql = """
        UPDATE dimension SET
            dimension_name = ?, description = ?, parent_name = ?, depth = ?, synonyms = ?,
            is_visible = ?, is_attribution = ?, enums = ?, updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (
        m.dimension_name, m.description, m.parent_name, m.depth, m.synonyms,
        _b(m.is_visible), _b(m.is_attribution), m.enums, now_iso(), dim_id,
    ))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, dim_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM dimension WHERE id = ? AND is_deleted = 0", (dim_id,)) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, dim_id: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE d.is_deleted = 0 AND d.id = ?", (dim_id,)) as cur:
        return await cur.fetchone()


async def exists_name_in_domain(db: aiosqlite.Connection, domain_id: int | None, name: str) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dimension WHERE domain_id IS ? AND dimension_name = ? AND is_deleted = 0",
        (domain_id, name),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


async def exists_name_in_domain_excluding(db, domain_id, name, dim_id) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dimension "
        "WHERE domain_id IS ? AND dimension_name = ? AND id <> ? AND is_deleted = 0",
        (domain_id, name, dim_id),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


def _build_where(datasource_id, domain_id, dimension_name, params: list) -> str:
    where = ["d.is_deleted = 0"]
    if datasource_id is not None:
        where.append("d.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("d.domain_id = ?"); params.append(domain_id)
    if dimension_name:
        where.append("d.dimension_name LIKE ?"); params.append(f"%{dimension_name}%")
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, dimension_name) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, dimension_name, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM dimension d WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, dimension_name, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, dimension_name, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY d.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, dim_id: int) -> int:
    cur = await db.execute(
        "UPDATE dimension SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), dim_id),
    )
    return cur.rowcount


# ---- 引用计数（删除校验用）----
async def count_bindings(db: aiosqlite.Connection, dim_id: int) -> int:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_dimension WHERE dimension_id = ? AND is_deleted = 0", (dim_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def count_values(db: aiosqlite.Connection, dim_id: int) -> int:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_dimension_value WHERE dimension_id = ? AND is_deleted = 0",
        (dim_id,),
    ) as cur:
        row = await cur.fetchone()
    return int(row["c"])
