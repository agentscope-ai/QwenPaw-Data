from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT d.id AS dataset_id, d.datasource_id, d.domain_id,
           s.datasource_name, b.domain_name,
           d.dataset_name, d.dataset_comment, d.dataset_type, d.sql_content, d.parents
    FROM dataset_meta d
    LEFT JOIN datasource s ON s.datasource_id = d.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = d.domain_id AND b.is_deleted = 0
"""


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO dataset_meta
            (datasource_id, domain_id, dataset_name, dataset_comment, dataset_type,
             sql_content, parents, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.datasource_id, m.domain_id, m.dataset_name, m.dataset_comment, m.dataset_type,
        m.sql_content, m.parents, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, dataset_id: int, m) -> int:
    sql = """
        UPDATE dataset_meta SET
            dataset_name = COALESCE(?, dataset_name), dataset_comment = COALESCE(?, dataset_comment),
            dataset_type = COALESCE(?, dataset_type), sql_content = COALESCE(?, sql_content),
            parents = COALESCE(?, parents), updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (
        m.dataset_name, m.dataset_comment, m.dataset_type, m.sql_content, m.parents,
        now_iso(), dataset_id,
    ))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, dataset_id: int) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM dataset_meta WHERE id = ? AND is_deleted = 0", (dataset_id,)
    ) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, dataset_id: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE d.is_deleted = 0 AND d.id = ?", (dataset_id,)) as cur:
        return await cur.fetchone()


async def exists_name_in_domain(db: aiosqlite.Connection, domain_id: int | None, name: str) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_meta WHERE domain_id IS ? AND dataset_name = ? AND is_deleted = 0",
        (domain_id, name),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


async def exists_name_in_domain_excluding(
    db: aiosqlite.Connection, domain_id: int | None, name: str, dataset_id: int
) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM dataset_meta "
        "WHERE domain_id IS ? AND dataset_name = ? AND id <> ? AND is_deleted = 0",
        (domain_id, name, dataset_id),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


def _build_where(datasource_id, domain_id, dataset_name, dataset_type, params: list) -> str:
    where = ["d.is_deleted = 0"]
    if datasource_id is not None:
        where.append("d.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("d.domain_id = ?"); params.append(domain_id)
    if dataset_name:
        where.append("d.dataset_name LIKE ?"); params.append(f"%{dataset_name}%")
    if dataset_type:
        where.append("d.dataset_type = ?"); params.append(dataset_type)
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, dataset_name, dataset_type) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_name, dataset_type, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM dataset_meta d WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, dataset_name, dataset_type, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, dataset_name, dataset_type, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY d.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, dataset_id: int) -> int:
    cur = await db.execute(
        "UPDATE dataset_meta SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), dataset_id),
    )
    return cur.rowcount


# ---- 引用计数（删除校验用）----
async def _count_child(db: aiosqlite.Connection, table: str, dataset_id: int) -> int:
    async with db.execute(
        f"SELECT COUNT(1) AS c FROM {table} WHERE dataset_id = ? AND is_deleted = 0", (dataset_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def count_columns(db, dataset_id):
    return await _count_child(db, "dataset_column_meta", dataset_id)


async def count_dimensions(db, dataset_id):
    return await _count_child(db, "dataset_dimension", dataset_id)


async def count_dimension_values(db, dataset_id):
    return await _count_child(db, "dataset_dimension_value", dataset_id)


async def count_formulas(db, dataset_id):
    return await _count_child(db, "metric_formula_lib", dataset_id)
