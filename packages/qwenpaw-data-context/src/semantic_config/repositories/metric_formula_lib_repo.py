from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

_RESPONSE_SELECT = """
    SELECT f.id, f.metric_id, m.metric_name, f.dataset_id, ds.dataset_name,
           f.datasource_id, f.domain_id, s.datasource_name, b.domain_name,
           f.formula, f.date_range, f.formula_evidence, f.derived_from, f.evidence_ext
    FROM metric_formula_lib f
    LEFT JOIN metric_lib m ON m.id = f.metric_id AND m.is_deleted = 0
    LEFT JOIN dataset_meta ds ON ds.id = f.dataset_id AND ds.is_deleted = 0
    LEFT JOIN datasource s ON s.datasource_id = f.datasource_id AND s.is_deleted = 0
    LEFT JOIN biz_domain b ON b.id = f.domain_id AND b.is_deleted = 0
"""


async def insert(db: aiosqlite.Connection, m) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO metric_formula_lib
            (metric_id, dataset_id, datasource_id, domain_id, formula, date_range,
             formula_evidence, derived_from, evidence_ext, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (
        m.metric_id, m.dataset_id, m.datasource_id, m.domain_id, m.formula, m.date_range,
        m.formula_evidence, m.derived_from, m.evidence_ext, ts, ts,
    )) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(db: aiosqlite.Connection, fid: int, m) -> int:
    sql = """
        UPDATE metric_formula_lib SET
            formula = COALESCE(?, formula), date_range = COALESCE(?, date_range),
            formula_evidence = COALESCE(?, formula_evidence), derived_from = COALESCE(?, derived_from),
            evidence_ext = COALESCE(?, evidence_ext), updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (
        m.formula, m.date_range, m.formula_evidence, m.derived_from, m.evidence_ext, now_iso(), fid,
    ))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, fid: int) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM metric_formula_lib WHERE id = ? AND is_deleted = 0", (fid,)
    ) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, fid: int) -> aiosqlite.Row | None:
    async with db.execute(_RESPONSE_SELECT + " WHERE f.is_deleted = 0 AND f.id = ?", (fid,)) as cur:
        return await cur.fetchone()


def _build_where(datasource_id, domain_id, metric_id, dataset_id, params: list) -> str:
    where = ["f.is_deleted = 0"]
    if datasource_id is not None:
        where.append("f.datasource_id = ?"); params.append(datasource_id)
    if domain_id is not None:
        where.append("f.domain_id = ?"); params.append(domain_id)
    if metric_id is not None:
        where.append("f.metric_id = ?"); params.append(metric_id)
    if dataset_id is not None:
        where.append("f.dataset_id = ?"); params.append(dataset_id)
    return " AND ".join(where)


async def count(db, datasource_id, domain_id, metric_id, dataset_id) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_id, metric_id, dataset_id, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM metric_formula_lib f WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(db, datasource_id, domain_id, metric_id, dataset_id, limit, off):
    params: list = []
    where = _build_where(datasource_id, domain_id, metric_id, dataset_id, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY f.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def find_all_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> list[aiosqlite.Row]:
    sql = _RESPONSE_SELECT + " WHERE f.is_deleted = 0 AND f.dataset_id = ? ORDER BY f.id"
    async with db.execute(sql, (dataset_id,)) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, fid: int) -> int:
    cur = await db.execute(
        "UPDATE metric_formula_lib SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
        (now_iso(), fid),
    )
    return cur.rowcount


async def soft_delete_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> int:
    cur = await db.execute(
        "UPDATE metric_formula_lib SET is_deleted = 1, updated_at = ? WHERE dataset_id = ? AND is_deleted = 0",
        (now_iso(), dataset_id),
    )
    return cur.rowcount
