"""用固定旧 DDL 验证真实启动能升级已有 staging 表并保留建模记录。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import profiling.db as probe_db
from profiling.repositories import staging_repo
from profiling.tables import staging_dimension, staging_metric


# 固定升级前的表头；不能从新版 metadata 删列构造，否则容易一起漏掉契约变化。
_LEGACY_DIMENSION_DDL = """
CREATE TABLE staging_dimension (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id TEXT NOT NULL,
    task_id TEXT,
    domain_id INTEGER,
    domain_name TEXT,
    dimension_name TEXT,
    description TEXT,
    synonyms TEXT DEFAULT '',
    dimension_role TEXT,
    origin TEXT DEFAULT 'create',
    source_type TEXT DEFAULT 'ALGO',
    probe_status TEXT DEFAULT 'PENDING',
    tenant_id TEXT,
    workspace_id TEXT,
    created_by TEXT,
    updated_by TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
)
"""

_LEGACY_METRIC_DDL = """
CREATE TABLE staging_metric (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource_id TEXT NOT NULL,
    task_id TEXT,
    domain_id INTEGER,
    domain_name TEXT,
    metric_name TEXT,
    description TEXT,
    synonyms TEXT,
    origin TEXT DEFAULT 'create',
    source_type TEXT DEFAULT 'ALGO',
    probe_status TEXT DEFAULT 'PENDING',
    tenant_id TEXT,
    workspace_id TEXT,
    created_by TEXT,
    updated_by TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
)
"""


@pytest.fixture(params=["sqlite", "postgresql"])
async def legacy_engine(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        monkeypatch.setattr(probe_db, "get_engine", lambda: engine)
        try:
            yield engine
        finally:
            await engine.dispose()
        return

    # 仅显式 opt-in 的专用测试库；不读取应用数据库配置或输出 DSN。
    url = os.environ.get("PROFILING_MIGRATION_TEST_PG_URL")
    if not url:
        pytest.skip(
            "set PROFILING_MIGRATION_TEST_PG_URL to an isolated PostgreSQL test DB"
        )
    schema = f"profiling_migration_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    # 建表编译必须按本次显式测试目标选择，避免开发环境的 Hologres 配置串入。
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", url)
    monkeypatch.setattr(probe_db, "get_engine", lambda: engine)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT current_schema()"))
                ).scalar_one() == schema
            yield engine
        finally:
            await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()
        await admin_engine.dispose()


def _legacy_ddl(engine, ddl):
    if engine.dialect.name == "postgresql":
        return ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return ddl


def _old_priority(engine):
    # PG/Hologres 分别 ADD 和 SET DEFAULT，旧行保持 NULL，由 promote._payload 兜底。
    return None if engine.dialect.name == "postgresql" else "MEDIUM"


async def _rows(engine, table):
    async with engine.connect() as connection:
        return [
            dict(row)
            for row in (
                await connection.execute(select(table).order_by(table.c.id))
            ).mappings()
        ]


async def test_legacy_startup_preserves_records_and_supports_repository(legacy_engine):
    async with legacy_engine.begin() as connection:
        await connection.execute(
            text(_legacy_ddl(legacy_engine, _LEGACY_DIMENSION_DDL))
        )
        await connection.execute(text(_legacy_ddl(legacy_engine, _LEGACY_METRIC_DDL)))
        await connection.execute(
            text("""
                INSERT INTO staging_dimension (
                    id, datasource_id, task_id, domain_id, domain_name,
                    dimension_name, description, synonyms, dimension_role,
                    origin, source_type, probe_status, tenant_id, workspace_id,
                    created_by, updated_by, is_deleted, created_at, updated_at
                ) VALUES (
                    :id, 'source-a', 'task-before-upgrade', 17, 'sales',
                    :name, 'original description', 'original synonym', :role,
                    'refine', 'HUMAN', :status, :tenant, 'workspace-a',
                    'creator', 'reviewer', :deleted, '2026-09-01', '2026-09-02'
                )
            """),
            [
                dict(
                    id=41,
                    name="订单日期",
                    role="time",
                    status="CONFIRMED",
                    tenant="tenant-a",
                    deleted=0,
                ),
                dict(
                    id=42,
                    name="地区",
                    role="categorical",
                    status="SKIPPED",
                    tenant="tenant-a",
                    deleted=0,
                ),
                dict(
                    id=43,
                    name="旧地区",
                    role=None,
                    status="PENDING",
                    tenant="tenant-a",
                    deleted=1,
                ),
                dict(
                    id=44,
                    name="其他租户地区",
                    role="categorical",
                    status="CONFIRMED",
                    tenant="tenant-b",
                    deleted=0,
                ),
            ],
        )
        await connection.execute(
            text("""
            INSERT INTO staging_metric (
                id, datasource_id, task_id, domain_id, domain_name,
                metric_name, description, synonyms, origin, source_type,
                probe_status, tenant_id, workspace_id, created_by, updated_by,
                is_deleted, created_at, updated_at
            ) VALUES (
                51, 'source-a', 'metric-task', 17, 'sales',
                '收入', 'original metric', 'revenue', 'refine', 'HUMAN',
                'CONFIRMED', 'tenant-a', 'workspace-a', 'creator', 'reviewer',
                0, '2026-09-01', '2026-09-02'
            )
        """)
        )
        old_dimensions = [
            dict(row)
            for row in (
                await connection.execute(
                    text("SELECT * FROM staging_dimension ORDER BY id")
                )
            ).mappings()
        ]
        old_metric = dict(
            (await connection.execute(text("SELECT * FROM staging_metric")))
            .mappings()
            .one()
        )
        if legacy_engine.dialect.name == "postgresql":
            # 固定历史 id 的 fixture 也要模拟已经推进的生产序列。
            await connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('staging_dimension', 'id'), 44)"
                )
            )
            await connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('staging_metric', 'id'), 51)"
                )
            )

    # 复现工作台所用 select(table) 在旧库上的缺列错误。
    async with legacy_engine.connect() as connection:
        with pytest.raises(SQLAlchemyError, match="dimension_type"):
            await connection.execute(select(staging_dimension))

    await probe_db.init_probe_db()
    dimensions = await _rows(legacy_engine, staging_dimension)
    for old, current in zip(old_dimensions, dimensions, strict=True):
        expected = dict(old)
        expected["dimension_type"] = expected.pop("dimension_role")
        expected.update(child_dimensions=None, priority=_old_priority(legacy_engine))
        assert current == expected
    assert await _rows(legacy_engine, staging_metric) == [
        {**old_metric, "priority": _old_priority(legacy_engine)}
    ]

    async with async_sessionmaker(legacy_engine)() as session:
        rows = await staging_repo.list_all(
            session, "staging_dimension", "source-a", "tenant-a", "workspace-a"
        )
        assert [(row["id"], row["dimension_type"]) for row in rows] == [
            (41, "time"),
            (42, "categorical"),
        ]
        metric = await staging_repo.find_by_id(
            session, "staging_metric", 51, "source-a", "tenant-a", "workspace-a"
        )
        assert metric["probe_status"] == "CONFIRMED"
        scope = dict(
            datasource_id="source-a",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            created_by="new-reviewer",
            task_id="task-after-upgrade",
        )
        dimension_id = await staging_repo.insert_row(
            session,
            "staging_dimension",
            {
                "dimension_name": "新地区",
                "dimension_type": "categorical",
                "child_dimensions": "省$$$市",
            },
            **scope,
        )
        metric_id = await staging_repo.insert_row(
            session, "staging_metric", {"metric_name": "订单数"}, **scope
        )
        assert dimension_id > 44
        assert metric_id > 51
        # 审核后可清空字段或设置自己的优先级，重启不应重新填默认值。
        await session.execute(
            update(staging_dimension)
            .where(staging_dimension.c.id == 41)
            .values(dimension_type=None, child_dimensions="", priority=None)
        )
        await session.execute(
            update(staging_metric)
            .where(staging_metric.c.id == 51)
            .values(priority="HIGH")
        )
        await session.commit()

    before_restart_dimensions = await _rows(legacy_engine, staging_dimension)
    before_restart_metrics = await _rows(legacy_engine, staging_metric)
    assert before_restart_dimensions[-1]["priority"] == "MEDIUM"
    assert before_restart_dimensions[-1]["child_dimensions"] == "省$$$市"
    assert before_restart_metrics[-1]["priority"] == "MEDIUM"
    await probe_db.init_probe_db()
    assert await _rows(legacy_engine, staging_dimension) == before_restart_dimensions
    assert await _rows(legacy_engine, staging_metric) == before_restart_metrics


@pytest.mark.parametrize("column_state", ["renamed", "both", "neither"])
async def test_partial_upgrade_preserves_existing_dimension_type(
    legacy_engine, column_state
):
    columns = {
        "renamed": "dimension_type TEXT,",
        "both": "dimension_role TEXT, dimension_type TEXT,",
        "neither": "",
    }
    async with legacy_engine.begin() as connection:
        await connection.execute(
            text(
                _legacy_ddl(
                    legacy_engine,
                    _LEGACY_DIMENSION_DDL.replace(
                        "dimension_role TEXT,", columns[column_state]
                    ),
                )
            )
        )
        await connection.execute(text(_legacy_ddl(legacy_engine, _LEGACY_METRIC_DDL)))
        await connection.execute(
            text("""
            INSERT INTO staging_dimension (id, datasource_id, dimension_name)
            VALUES (1, 'source-a', 'current value'), (2, 'source-a', 'explicit null')
        """)
        )
        if column_state != "neither":
            await connection.execute(
                text(
                    "UPDATE staging_dimension SET dimension_type = 'categorical' WHERE id = 1"
                )
            )
        if column_state == "both":
            await connection.execute(
                text("UPDATE staging_dimension SET dimension_role = 'time'")
            )

    await probe_db.init_probe_db()
    rows = await _rows(legacy_engine, staging_dimension)
    assert [row["dimension_type"] for row in rows] == (
        [None, None] if column_state == "neither" else ["categorical", None]
    )
    assert all(row["priority"] == _old_priority(legacy_engine) for row in rows)
    assert all(row["child_dimensions"] is None for row in rows)
    # 既有新列（包括 NULL）优先；两列并存时旧值不能在任何一次启动时覆盖它。
    await probe_db.init_probe_db()
    assert await _rows(legacy_engine, staging_dimension) == rows


async def test_concurrent_migrations_preserve_legacy_rows(legacy_engine):
    from profiling.schema_migration import migrate_legacy_probe_schema

    async with legacy_engine.begin() as connection:
        await connection.execute(
            text(_legacy_ddl(legacy_engine, _LEGACY_DIMENSION_DDL))
        )
        await connection.execute(text(_legacy_ddl(legacy_engine, _LEGACY_METRIC_DDL)))
        await connection.execute(
            text("""
            INSERT INTO staging_dimension (datasource_id, dimension_name, dimension_role)
            VALUES ('source-a', '订单日期', 'time')
        """)
        )

    # 多进程启动会独立判断并补列；此处仅并发已有表升级，不混入首次建表竞争。
    await asyncio.gather(
        *(migrate_legacy_probe_schema(legacy_engine) for _ in range(4))
    )
    await probe_db.init_probe_db()
    rows = await _rows(legacy_engine, staging_dimension)
    assert len(rows) == 1
    assert rows[0]["dimension_type"] == "time"
    assert rows[0]["priority"] == _old_priority(legacy_engine)
    assert await _rows(legacy_engine, staging_metric) == []


async def test_pg_startup_recovers_priority_default_after_interrupted_upgrade(
    legacy_engine,
):
    if legacy_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL migration sets the default in a separate DDL statement")
    async with legacy_engine.begin() as connection:
        await connection.execute(
            text(_legacy_ddl(legacy_engine, _LEGACY_DIMENSION_DDL))
        )
        await connection.execute(text(_legacy_ddl(legacy_engine, _LEGACY_METRIC_DDL)))
        # 模拟 ADD 已提交，但 SET DEFAULT 尚未执行时服务中断。
        await connection.execute(
            text("ALTER TABLE staging_dimension ADD COLUMN priority TEXT")
        )
        await connection.execute(
            text("ALTER TABLE staging_metric ADD COLUMN priority TEXT")
        )
        await connection.execute(
            text("""
            INSERT INTO staging_dimension (datasource_id, dimension_name, dimension_role)
            VALUES ('source-a', 'existing dimension', 'time')
        """)
        )
        await connection.execute(
            text("""
            INSERT INTO staging_metric (datasource_id, metric_name)
            VALUES ('source-a', 'existing metric')
        """)
        )

    await probe_db.init_probe_db()
    async with async_sessionmaker(legacy_engine)() as session:
        scope = dict(
            datasource_id="source-a",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            created_by="reviewer",
            task_id="after-recovery",
        )
        await staging_repo.insert_row(
            session, "staging_dimension", {"dimension_name": "new dimension"}, **scope
        )
        await staging_repo.insert_row(
            session, "staging_metric", {"metric_name": "new metric"}, **scope
        )
        await session.commit()

    for table in (staging_dimension, staging_metric):
        rows = await _rows(legacy_engine, table)
        assert [row["priority"] for row in rows] == [None, "MEDIUM"]
