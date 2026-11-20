import aiosqlite
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import get_session_factory, reset_engine


def test_inflight_cm_jobs_are_copied_into_cm1_tables(tmp_path):
    from sqlalchemy import create_engine

    from context_manager.db import CMDatabase
    from context_manager.settings.db import DbSettings

    url = f"sqlite:///{tmp_path / 'copy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cm_job (
                    job_kind TEXT NOT NULL,
                    job_key TEXT NOT NULL,
                    tenant_id TEXT,
                    workspace_id TEXT,
                    user_id TEXT,
                    status TEXT NOT NULL,
                    progress TEXT,
                    error_msg TEXT,
                    heartbeat_at DOUBLE,
                    started_at DOUBLE,
                    executor_pod TEXT,
                    resume_count INTEGER,
                    PRIMARY KEY (job_kind, job_key)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cm_job VALUES
                    ('vector_rebuild', 't:w', 't', 'w', 'u', 'running',
                     '{"job_id":"job-1","phase":"embedding","config_snapshot":{"model":"m"}}',
                     NULL, 1, NULL, 'pod-a', 1),
                    ('doc_ingest', 'document:abc', 't', 'w', 'u', 'building',
                     '{"doc_id":"document:abc"}', NULL, NULL, NULL, NULL, 0)
                """
            )
        )
    engine.dispose()
    database = CMDatabase.from_settings(DbSettings(url=url))
    try:
        database.initialize()
        with database.engine.connect() as connection:
            rebuild = (
                connection.execute(
                    text("SELECT status, resume_count, job_id FROM cm_embedding_jobs")
                )
                .mappings()
                .one()
            )
            document = (
                connection.execute(
                    text("SELECT status, document_id FROM cm_kg_documents")
                )
                .mappings()
                .one()
            )
            claim = connection.execute(
                text("SELECT COUNT(*) FROM cm_kg_user_claim")
            ).scalar_one()
        assert rebuild["job_id"] == "job-1"
        assert rebuild["status"] == "running"
        assert rebuild["resume_count"] == 1
        assert document["document_id"] == "document:abc"
        assert document["status"] == "failed"
        assert claim == 0
    finally:
        database.close()


def test_orphan_sweep_removes_expired_sql_downloads_only(tmp_path, monkeypatch):
    from context_manager.heartbeat.sweep import _sweep_expired_csv

    expired, current, session = _plant_expired_download(tmp_path, monkeypatch)
    _sweep_expired_csv()
    assert not expired.exists()
    assert current.exists()
    assert session.exists()


def _plant_expired_download(tmp_path, monkeypatch):
    import os
    import time

    from context_manager.settings import get_settings as cm_settings

    downloads = tmp_path / "tenant" / "workspace" / ".cm" / "sql_downloads"
    downloads.mkdir(parents=True)
    expired = downloads / "old.csv"
    current = downloads / "new.csv"
    expired.write_text("old")
    current.write_text("new")
    session = tmp_path / "sessions" / "body.json"
    session.parent.mkdir()
    session.write_text("{}")
    old = time.time() - 25 * 60 * 60
    os.utime(expired, (old, old))
    os.utime(session, (old, old))
    monkeypatch.setattr(cm_settings().paths, "home", tmp_path)
    return expired, current, session


async def test_async_engine_sweep_fails_stale_rows_when_sync_pool_is_down(
    tmp_path, monkeypatch
):
    from context_manager.heartbeat.sweep import _sweep_once

    expired, current, session = _plant_expired_download(tmp_path, monkeypatch)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")

    def unavailable():
        raise RuntimeError("sync pool unavailable")

    monkeypatch.setattr("context_manager.db.get_database", unavailable)
    monkeypatch.setattr("semantic_config.engine.get_engine", lambda: engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE weave_task ("
                    "task_id TEXT, status TEXT, is_deleted INTEGER NOT NULL, "
                    "heartbeat_at FLOAT, error_msg TEXT)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO weave_task "
                    "(task_id, status, is_deleted, heartbeat_at) "
                    "VALUES ('stale', 'RUNNING', 0, NULL)"
                )
            )
        await _sweep_once()
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, error_msg FROM weave_task WHERE task_id = 'stale'"
                    )
                )
            ).one()
        assert row == ("FAILED", "执行中断")
        assert not expired.exists()
        assert current.exists()
        assert session.exists()
    finally:
        await engine.dispose()


async def test_csv_cleanup_runs_when_both_database_scans_fail(tmp_path, monkeypatch):
    from context_manager.heartbeat.sweep import _sweep_once

    expired, current, session = _plant_expired_download(tmp_path, monkeypatch)

    def unavailable():
        raise RuntimeError("control plane unavailable")

    monkeypatch.setattr("context_manager.db.get_database", unavailable)
    monkeypatch.setattr("semantic_config.engine.get_engine", unavailable)
    await _sweep_once()
    assert not expired.exists()
    assert current.exists()
    assert session.exists()


async def test_existing_weave_task_gains_heartbeat_columns(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    monkeypatch.setattr(get_settings(), "db_url", url)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE weave_task (id INTEGER PRIMARY KEY, task_id TEXT, "
                    "status TEXT, is_deleted INTEGER NOT NULL DEFAULT 0)"
                )
            )
        await init_db()
        async with get_session_factory()() as db:
            rows = (await db.execute(text("PRAGMA table_info(weave_task)"))).all()
        assert {"heartbeat_at", "executor_pod"} <= {row[1] for row in rows}
    finally:
        await reset_engine()
        await engine.dispose()


async def test_migrates_internal_ids_and_keeps_external_ids(tmp_path, monkeypatch):
    db_path = tmp_path / "semantic_config.db"
    monkeypatch.setattr(get_settings(), "db_url", f"sqlite+aiosqlite:///{db_path}")
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE datasource (
                id INTEGER PRIMARY KEY,
                datasource_id TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO datasource (id, datasource_id) VALUES
                (7, 'postgresql-d28b5786'),
                (8, 'postgresql-current');
            """
        )
        for table in (
            "biz_domain",
            "dataset_meta",
            "dataset_column_meta",
            "dimension",
            "dataset_dimension",
            "dataset_dimension_value",
            "metric_lib",
            "metric_formula_lib",
            "weave_task",
        ):
            await conn.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, datasource_id INTEGER)"
            )
            await conn.execute(f"INSERT INTO {table} (id, datasource_id) VALUES (1, 7)")
            await conn.execute(
                f"INSERT INTO {table} (id, datasource_id) VALUES (2, 'postgresql-current')"
            )

        await conn.commit()
        try:
            await init_db()
            await init_db()
        finally:
            await reset_engine()

        for table in (
            "biz_domain",
            "dataset_meta",
            "dataset_column_meta",
            "dimension",
            "dataset_dimension",
            "dataset_dimension_value",
            "metric_lib",
            "metric_formula_lib",
            "weave_task",
        ):
            async with conn.execute(
                f"SELECT datasource_id, typeof(datasource_id) FROM {table} ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
            assert rows == [
                ("postgresql-d28b5786", "text"),
                ("postgresql-current", "text"),
            ]
