"""Mark heartbeat-stale business rows failed. Safe for several pods at once."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import exists, or_, select, update

from context_manager.settings import get_settings

from .refresh import stale_cutoff

log = logging.getLogger(__name__)


def _cutoff() -> float:
    return stale_cutoff()


def sweep_connection(connection) -> None:
    """Sweep whatever of the stateless tables this connection can see."""
    from sqlalchemy import inspect

    names = set(inspect(connection).get_table_names())
    cutoff = _cutoff()
    if "weave_task" in names:
        from semantic_config.tables import weave_task

        # An open recovery with a fresh heartbeat still owns the weave receipt.
        # Failing the task here would discard a later success projection.
        live_recovery = ()
        if "cm_recovery_runs" in names:
            from context_manager.schema.relational import recovery_runs

            live_recovery = (
                ~exists(
                    select(recovery_runs.c.task_id).where(
                        recovery_runs.c.tenant_id == weave_task.c.tenant_id,
                        recovery_runs.c.workspace_id == weave_task.c.workspace_id,
                        recovery_runs.c.task_type == "weave",
                        recovery_runs.c.task_id == weave_task.c.task_id,
                        recovery_runs.c.recovery_state != "CLOSED",
                        recovery_runs.c.heartbeat_at >= cutoff,
                    )
                ),
            )
        connection.execute(
            update(weave_task)
            .where(
                weave_task.c.status == "RUNNING",
                weave_task.c.is_deleted == 0,
                or_(
                    weave_task.c.heartbeat_at.is_(None),
                    weave_task.c.heartbeat_at < cutoff,
                ),
                *live_recovery,
            )
            .values(status="FAILED", error_msg="执行中断")
        )
    if "probe_task" in names:
        from profiling.tables import probe_task

        connection.execute(
            update(probe_task)
            .where(
                probe_task.c.status == "RUNNING",
                probe_task.c.is_deleted == 0,
                or_(
                    probe_task.c.heartbeat_at.is_(None),
                    probe_task.c.heartbeat_at < cutoff,
                ),
            )
            .values(status="FAILED", error_msg="执行中断")
        )
        _restore_probe_stages(connection)
    if "explore_scope" in names:
        from semantic_config.tables import explore_scope

        connection.execute(
            update(explore_scope)
            .where(
                explore_scope.c.last_status == "running",
                explore_scope.c.is_deleted == 0,
                or_(
                    explore_scope.c.heartbeat_at.is_(None),
                    explore_scope.c.heartbeat_at < cutoff,
                ),
            )
            .values(last_status="failed", last_error="执行中断", heartbeat_at=None)
        )
    if "cm_kg_user_claim" in names and "cm_kg_documents" in names:
        from context_manager.schema.relational import documents, kg_user_claims

        released = (
            connection.execute(
                update(kg_user_claims)
                .where(
                    kg_user_claims.c.heartbeat_at.is_not(None),
                    kg_user_claims.c.heartbeat_at < cutoff,
                )
                .values(heartbeat_at=None, executor_pod=None)
                .returning(
                    kg_user_claims.c.tenant_id,
                    kg_user_claims.c.workspace_id,
                    kg_user_claims.c.user_id,
                    kg_user_claims.c.run_epoch,
                )
            )
            .mappings()
            .all()
        )
        for row in released:
            connection.execute(
                update(documents)
                .where(
                    documents.c.tenant_id == row["tenant_id"],
                    documents.c.workspace_id == row["workspace_id"],
                    documents.c.user_id == row["user_id"],
                    documents.c.run_epoch == row["run_epoch"],
                    documents.c.status == "processing",
                )
                .values(status="failed", ingest_error="执行中断")
            )


def _restore_probe_stages(connection) -> None:
    from profiling.stages import STAGE_DEFS
    from profiling.tables import probe_stage, probe_task
    from semantic_config.tables import metadata

    rows = (
        connection.execute(
            select(probe_stage).where(
                probe_stage.c.status == "PROBING",
                probe_stage.c.is_deleted == 0,
            )
        )
        .mappings()
        .all()
    )
    for stage in rows:
        task = None
        if stage["current_task_id"]:
            task = (
                connection.execute(
                    select(probe_task.c.status, probe_task.c.heartbeat_at).where(
                        probe_task.c.task_id == stage["current_task_id"],
                        probe_task.c.is_deleted == 0,
                    )
                )
                .mappings()
                .first()
            )
        if task is not None and task["status"] == "RUNNING":
            continue
        definition = STAGE_DEFS.get(stage["stage"])
        if definition is None:
            continue
        table = metadata.tables[definition.table]
        has_rows = (
            connection.execute(
                select(table.c.id)
                .where(
                    table.c.datasource_id == stage["datasource_id"],
                    table.c.tenant_id == stage["tenant_id"],
                    table.c.workspace_id == stage["workspace_id"],
                    table.c.is_deleted == 0,
                )
                .limit(1)
            ).first()
            is not None
        )
        restored = "REVIEWING" if has_rows else "NOT_STARTED"
        task_id = stage["current_task_id"]
        match = (
            probe_stage.c.current_task_id.is_(None)
            if task_id is None
            else probe_stage.c.current_task_id == task_id
        )
        connection.execute(
            update(probe_stage)
            .where(
                probe_stage.c.datasource_id == stage["datasource_id"],
                probe_stage.c.stage == stage["stage"],
                probe_stage.c.tenant_id == stage["tenant_id"],
                probe_stage.c.workspace_id == stage["workspace_id"],
                match,
                probe_stage.c.status == "PROBING",
                probe_stage.c.is_deleted == 0,
            )
            .values(status=restored)
        )


async def run_orphan_sweep(stop: asyncio.Event) -> None:
    """Scan on the deployment sweep interval until ``stop`` is set."""
    while not stop.is_set():
        await _sweep_once()
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=get_settings().heartbeat.sweep_interval_seconds,
            )
        except TimeoutError:
            continue


async def _sweep_once() -> None:
    """Scan the shared database, then delete expired SQL downloads.

    Fall back to the async pool if the sync pool fails. File cleanup runs
    independently of database failures.
    """
    try:
        await asyncio.to_thread(_sweep_sync_engine)
    except Exception:
        log.exception("orphan sweep sync scan failed")
        try:
            await _sweep_async_engine()
        except Exception:
            log.exception("orphan sweep async scan failed")
    try:
        await asyncio.to_thread(_sweep_expired_csv)
    except Exception:
        log.exception("orphan sweep csv cleanup failed")


def _sweep_sync_engine() -> None:
    """Sweep through the synchronous CM pool from a worker thread."""
    from context_manager.db import get_database

    with get_database().engine.begin() as connection:
        sweep_connection(connection)


async def _sweep_async_engine() -> None:
    """Sweep through the async pool on the loop that owns it.

    The Semantic Config pool is asyncpg or aiosqlite. ``run_sync`` is the
    bridge that may call ``sweep_connection``; using ``sync_engine`` from a
    worker thread raises MissingGreenlet.
    """
    from semantic_config.engine import get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(sweep_connection)


def _sweep_expired_csv() -> None:
    """Delete expired SQL downloads. Session files are left alone."""
    from context_manager.settings import get_settings

    settings = get_settings()
    root = settings.paths.home
    if not root.is_dir():
        return
    root = root.resolve()
    cutoff = time.time() - settings.sql.retention_seconds
    for directory in root.rglob("sql_downloads"):
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if not resolved.is_dir() or not resolved.is_relative_to(root):
            continue
        for csv_path in resolved.glob("*.csv"):
            try:
                if csv_path.is_symlink() or not csv_path.is_file():
                    continue
                if csv_path.stat().st_mtime <= cutoff:
                    csv_path.unlink()
            except OSError:
                log.debug("orphan sweep could not remove %s", csv_path, exc_info=True)
