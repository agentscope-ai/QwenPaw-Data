from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT m.id, m.datasource_id, m.domain_id,
           s.datasource_name, b.domain_name,
           m.metric_name, m.description, m.unit,
           m.is_polaris, m.show_distribution, m.is_visible, m.synonyms, m.tags
    FROM metric_lib m
    LEFT JOIN datasource s ON s.datasource_id = m.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = m.domain_id AND b.is_deleted = 0
"""


def _b(v):
    return None if v is None else (1 if v else 0)


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO metric_lib
            (datasource_id, domain_id, metric_name, description, unit,
             is_polaris, show_distribution, is_visible, synonyms, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.datasource_id, m.domain_id, m.metric_name, m.description, m.unit,
        _b(m.is_polaris), _b(m.show_distribution), _b(m.is_visible), m.synonyms, m.tags, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, metric_id: int, m) -> int:
    sql = """
        UPDATE metric_lib SET
            metric_name = COALESCE(?, metric_name), description = COALESCE(?, description),
            unit = COALESCE(?, unit), is_polaris = COALESCE(?, is_polaris),
            show_distribution = COALESCE(?, show_distribution),
            is_visible = COALESCE(?, is_visible), synonyms = COALESCE(?, synonyms),
            tags = COALESCE(?, tags), updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (
        m.metric_name, m.description, m.unit, _b(m.is_polaris), _b(m.show_distribution),
        _b(m.is_visible), m.synonyms, m.tags, now_iso(), metric_id,
    ))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, metric_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM metric_lib WHERE id = ? AND is_deleted = 0", (metric_id,)) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, metric_id: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE m.is_deleted = 0 AND m.id = ?", (metric_id,)) as cur:
        return await cur.fetchone()


async def exists_name_in_domain(db: aiosqlite.Connection, domain_id: int | None, name: str) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM metric_lib WHERE domain_id IS ? AND metric_name = ? AND is_deleted = 0",
        (domain_id, name),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


async def exists_name_in_domain_excluding(db, domain_id, name, metric_id) -> bool:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM metric_lib "
        "WHERE domain_id IS ? AND metric_name = ? AND id <> ? AND is_deleted = 0",
        (domain_id, name, metric_id),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


def _build_where(datasource_id, domain_id, metric_name, params: list) -> str:
    where = ["m.is_deleted = 0"]
    if datasource_id is not None:
        where.append("m.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("m.domain_id = ?"); params.append(domain_id)
    if metric_name:
        where.append("m.metric_name LIKE ?"); params.append(f"%{metric_name}%")
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, metric_name) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, metric_name, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM metric_lib m WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, metric_name, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, metric_name, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY m.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, metric_id: int) -> int:
    cur = await db.execute(
        "UPDATE metric_lib SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), metric_id),
    )
    return cur.rowcount


async def count_formulas(db: aiosqlite.Connection, metric_id: int) -> int:
    async with db.execute(
        "SELECT COUNT(1) AS c FROM metric_formula_lib WHERE metric_id = ? AND is_deleted = 0", (metric_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row["c"])
