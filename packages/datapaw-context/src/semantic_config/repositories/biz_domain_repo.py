from __future__ import annotations

import aiosqlite

from semantic_config.db import now_iso

# 列表/详情统一 JOIN datasource 派生展示名（datasource_name 可为 null）
# 注：b.datasource_id 存数据源对外编码（TEXT），关联 datasource.datasource_id（非内部主键 id）。
_RESPONSE_SELECT = """
    SELECT b.id AS domain_id, b.datasource_id,
           s.datasource_name,
           b.domain_name, b.display_name, b.description, b.aliases
    FROM biz_domain b
    LEFT JOIN datasource s ON s.datasource_id = b.datasource_id AND s.is_deleted = 0
"""


async def insert(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    domain_name: str | None,
    display_name: str | None,
    description: str | None,
    aliases: str | None,
) -> int:
    ts = now_iso()
    sql = """
        INSERT INTO biz_domain
            (datasource_id, domain_name, display_name, description, aliases, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """
    async with db.execute(sql, (datasource_id, domain_name, display_name, description, aliases, ts, ts)) as cur:
        row = await cur.fetchone()
    return int(row["id"])


async def update(
    db: aiosqlite.Connection,
    domain_id: int,
    domain_name: str | None,
    display_name: str | None,
    description: str | None,
    aliases: str | None,
) -> int:
    sql = """
        UPDATE biz_domain SET
            domain_name = COALESCE(?, domain_name), display_name = COALESCE(?, display_name),
            description = COALESCE(?, description), aliases = COALESCE(?, aliases), updated_at = ?
        WHERE id = ? AND is_deleted = 0
    """
    cur = await db.execute(sql, (domain_name, display_name, description, aliases, now_iso(), domain_id))
    return cur.rowcount


async def find_by_id(db: aiosqlite.Connection, domain_id: int) -> aiosqlite.Row | None:
    sql = "SELECT * FROM biz_domain WHERE id = ? AND is_deleted = 0"
    async with db.execute(sql, (domain_id,)) as cur:
        return await cur.fetchone()


async def find_response_by_id(db: aiosqlite.Connection, domain_id: int) -> aiosqlite.Row | None:
    sql = _RESPONSE_SELECT + " WHERE b.is_deleted = 0 AND b.id = ?"
    async with db.execute(sql, (domain_id,)) as cur:
        return await cur.fetchone()


async def exists_by_ds_and_name(db: aiosqlite.Connection, datasource_id: str, domain_name: str) -> bool:
    sql = ("SELECT COUNT(1) AS c FROM biz_domain "
           "WHERE datasource_id = ? AND domain_name = ? AND is_deleted = 0")
    async with db.execute(sql, (datasource_id, domain_name)) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


async def exists_by_ds_and_name_excluding(
    db: aiosqlite.Connection, datasource_id: str, domain_name: str, domain_id: int
) -> bool:
    sql = ("SELECT COUNT(1) AS c FROM biz_domain "
           "WHERE datasource_id = ? AND domain_name = ? AND id <> ? AND is_deleted = 0")
    async with db.execute(sql, (datasource_id, domain_name, domain_id)) as cur:
        row = await cur.fetchone()
    return row["c"] > 0


def _build_where(datasource_id: str | None, domain_name: str | None, params: list) -> str:
    where = ["b.is_deleted = 0"]
    if datasource_id is not None:
        where.append("b.datasource_id = ?")
        params.append(datasource_id)
    if domain_name:
        where.append("b.domain_name LIKE ?")
        params.append(f"%{domain_name}%")
    return " AND ".join(where)


async def count(db: aiosqlite.Connection, datasource_id: str | None, domain_name: str | None) -> int:
    params: list = []
    where = _build_where(datasource_id, domain_name, params)
    async with db.execute(f"SELECT COUNT(1) AS c FROM biz_domain b WHERE {where}", params) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def find_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    domain_name: str | None,
    limit: int,
    off: int,
) -> list[aiosqlite.Row]:
    params: list = []
    where = _build_where(datasource_id, domain_name, params)
    sql = _RESPONSE_SELECT + f" WHERE {where} ORDER BY b.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, off])
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def soft_delete(db: aiosqlite.Connection, domain_id: int) -> int:
    sql = "UPDATE biz_domain SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0"
    cur = await db.execute(sql, (now_iso(), domain_id))
    return cur.rowcount


# ---- 引用计数（删除校验用）：域下数据集/维度/指标 ----
async def _count(db: aiosqlite.Connection, table: str, domain_id: int) -> int:
    async with db.execute(
        f"SELECT COUNT(1) AS c FROM {table} WHERE domain_id = ? AND is_deleted = 0", (domain_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row["c"])


async def count_datasets(db: aiosqlite.Connection, domain_id: int) -> int:
    return await _count(db, "dataset_meta", domain_id)


async def count_dimensions(db: aiosqlite.Connection, domain_id: int) -> int:
    return await _count(db, "dimension", domain_id)


async def count_metrics(db: aiosqlite.Connection, domain_id: int) -> int:
    return await _count(db, "metric_lib", domain_id)
