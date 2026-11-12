"""Add heartbeat columns on tables that develop already deployed.

``dev/cm-1.0`` itself was never deployed. Tables that exist only in that
code are created in their final shape. ``weave_task``, ``probe_task``,
``explore_scope`` and ``explore_state`` may already be in the develop
database, so startup adds the missing columns and collapses duplicate
live rows before the partial unique indexes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

log = logging.getLogger(__name__)

_FLOAT = {"postgresql": "DOUBLE PRECISION", "sqlite": "DOUBLE"}


def _has_column(conn: Connection, table: str, column: str) -> bool:
    if not inspect(conn).has_table(table):
        return False
    return column in {item["name"] for item in inspect(conn).get_columns(table)}


def _add_column(conn: Connection, table: str, column: str, ddl: str) -> None:
    if not inspect(conn).has_table(table) or _has_column(conn, table, column):
        return
    try:
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    except DBAPIError as exc:
        if not _duplicate_column(conn, exc):
            raise
    log.info("stateless schema added %s.%s", table, column)


def _duplicate_column(conn: Connection, exc: DBAPIError) -> bool:
    if conn.dialect.name == "postgresql":
        return getattr(exc.orig, "sqlstate", None) == "42701"
    message = str(getattr(exc, "orig", exc)).lower()
    return conn.dialect.name == "sqlite" and "duplicate column" in message


def _dedupe(conn: Connection, table: str, keys: tuple[str, ...]) -> None:
    if not _has_column(conn, table, "is_deleted") or not _has_column(conn, table, "id"):
        return
    grouped = ", ".join(keys)
    rows = conn.execute(
        text(
            f"SELECT {grouped}, MIN(id) AS keep_id FROM {table} "
            f"WHERE is_deleted = 0 GROUP BY {grouped} HAVING COUNT(*) > 1"
        )
    ).all()
    for row in rows:
        *values, keep_id = row
        where = " AND ".join(f"{key} = :{key}" for key in keys)
        params = dict(zip(keys, values))
        params["keep_id"] = keep_id
        conn.execute(
            text(
                f"UPDATE {table} SET is_deleted = 1 WHERE {where} "
                "AND is_deleted = 0 AND id <> :keep_id"
            ),
            params,
        )


def _partial_unique(
    conn: Connection, name: str, table: str, keys: tuple[str, ...]
) -> None:
    if "hologres.aliyuncs.com" in (conn.engine.url.host or ""):
        return
    if not _has_column(conn, table, "is_deleted"):
        return
    conn.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(keys)}) "
            "WHERE is_deleted = 0"
        )
    )


def ensure_recovery_run_columns(conn: Connection) -> None:
    """Add ownership columns when ``cm_recovery_runs`` already exists.

    New databases create the columns with the table. This repair runs before
    the startup missing-column check so an already deployed table can gain
    them without a second schema owner.
    """
    float_type = _FLOAT.get(conn.dialect.name, "DOUBLE PRECISION")
    _add_column(conn, "cm_recovery_runs", "heartbeat_at", float_type)
    _add_column(conn, "cm_recovery_runs", "executor_pod", "TEXT")


def ensure_deployed_stateless_columns(conn: Connection) -> None:
    """Idempotent startup repair for the develop control-plane tables."""
    float_type = _FLOAT.get(conn.dialect.name, "DOUBLE PRECISION")
    for table in ("weave_task", "probe_task", "explore_scope"):
        _add_column(conn, table, "heartbeat_at", float_type)
        _add_column(conn, table, "executor_pod", "TEXT")
    _add_column(conn, "weave_task", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "explore_scope", "run_epoch", "INTEGER NOT NULL DEFAULT 0")
    scope_keys = ("tenant_id", "workspace_id", "datasource_id")
    state_keys = (*scope_keys, "table_name")
    _dedupe(conn, "explore_scope", scope_keys)
    _dedupe(conn, "explore_state", state_keys)
    _partial_unique(conn, "uq_explore_scope_ds", "explore_scope", scope_keys)
    _partial_unique(conn, "uq_explore_state_tbl", "explore_state", state_keys)


def copy_inflight_cm_jobs(conn: Connection) -> None:
    """Copy develop's in-progress document and rebuild rows into CM 1.0 tables.

    The new process does not write ``cm_job``. Rows already copied are left
    as they are. A fresh heartbeat keeps the document claim; a stale one is
    stored as failed so the next claim can start again.
    """
    if not inspect(conn).has_table("cm_job"):
        return
    columns = {item["name"] for item in inspect(conn).get_columns("cm_job")}
    selected = [
        name
        for name in (
            "job_kind",
            "job_key",
            "tenant_id",
            "workspace_id",
            "user_id",
            "status",
            "progress",
            "error_msg",
            "heartbeat_at",
            "started_at",
            "executor_pod",
            "resume_count",
        )
        if name in columns
    ]
    if (
        "job_kind" not in selected
        or "job_key" not in selected
        or "status" not in selected
    ):
        return
    rows = conn.execute(
        text(
            f"SELECT {', '.join(selected)} FROM cm_job "
            "WHERE job_kind IN ('doc_ingest', 'vector_rebuild') "
            "AND status IN ('building', 'running', 'pending', 'processing')"
        )
    ).mappings()
    now = time.time()
    stamp = datetime.now(UTC).isoformat()
    for row in rows:
        payload = _json_object(row.get("progress"))
        heartbeat = row.get("heartbeat_at")
        fresh = heartbeat is not None and now - float(heartbeat) <= 300
        kind = row["job_kind"]
        if kind == "doc_ingest" and inspect(conn).has_table("cm_kg_documents"):
            _copy_document(conn, row, payload, fresh, stamp)
        elif kind == "vector_rebuild" and inspect(conn).has_table("cm_embedding_jobs"):
            _copy_rebuild(conn, row, payload, stamp)


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _copy_document(conn, row, payload: dict, fresh: bool, stamp: str) -> None:
    tenant_id = row.get("tenant_id") or ""
    workspace_id = row.get("workspace_id") or ""
    user_id = row.get("user_id") or ""
    document_id = str(payload.get("doc_id") or row.get("job_key") or "")
    if not tenant_id or not workspace_id or not user_id or not document_id:
        return
    filename = str(payload.get("filename") or document_id)
    status = "processing" if fresh else "failed"
    conn.execute(
        text(
            "INSERT INTO cm_kg_documents ("
            "tenant_id, workspace_id, user_id, filename, document_id, "
            "byte_size, status, created_at, updated_at, ingest_error, run_epoch"
            ") VALUES ("
            ":tenant_id, :workspace_id, :user_id, :filename, :document_id, "
            "0, :status, :stamp, :stamp, :error, 1"
            ") ON CONFLICT (tenant_id, workspace_id, user_id, filename) DO NOTHING"
        ),
        {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "filename": filename,
            "document_id": document_id,
            "status": status,
            "stamp": stamp,
            "error": None if fresh else "执行中断",
        },
    )
    if not fresh or not inspect(conn).has_table("cm_kg_user_claim"):
        return
    conn.execute(
        text(
            "INSERT INTO cm_kg_user_claim ("
            "tenant_id, workspace_id, user_id, heartbeat_at, executor_pod, run_epoch"
            ") VALUES ("
            ":tenant_id, :workspace_id, :user_id, :heartbeat_at, :executor_pod, 1"
            ") ON CONFLICT (tenant_id, workspace_id, user_id) DO NOTHING"
        ),
        {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "heartbeat_at": row.get("heartbeat_at"),
            "executor_pod": row.get("executor_pod"),
        },
    )


def _copy_rebuild(conn, row, payload: dict, stamp: str) -> None:
    tenant_id = row.get("tenant_id") or payload.get("tenant_id") or ""
    workspace_id = row.get("workspace_id") or payload.get("workspace_id") or ""
    job_id = str(payload.get("job_id") or "")
    if not tenant_id or not workspace_id or not job_id:
        return
    progress = {
        "phase": payload.get("phase") or payload.get("status") or "pending",
        "current_label": payload.get("current_label") or "",
        "labels_done": payload.get("labels_done") or 0,
        "labels_total": payload.get("labels_total") or 0,
    }
    config = payload.get("config_snapshot") or {}
    conn.execute(
        text(
            "INSERT INTO cm_embedding_jobs ("
            "tenant_id, workspace_id, job_id, created_by, status, created_at, "
            "attempted_at, error, progress, config_snapshot, heartbeat_at, "
            "executor_pod, resume_count"
            ") VALUES ("
            ":tenant_id, :workspace_id, :job_id, :created_by, :status, :created_at, "
            ":attempted_at, :error, :progress, :config_snapshot, :heartbeat_at, "
            ":executor_pod, :resume_count"
            ") ON CONFLICT (tenant_id, workspace_id, job_id) DO NOTHING"
        ),
        {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "job_id": job_id,
            "created_by": row.get("user_id") or payload.get("created_by") or "",
            "status": row.get("status")
            if row.get("status") in ("pending", "running")
            else "pending",
            "created_at": payload.get("created_at") or stamp,
            "attempted_at": payload.get("created_at") or stamp,
            "error": row.get("error_msg"),
            "progress": json.dumps(progress, ensure_ascii=False),
            "config_snapshot": json.dumps(config, ensure_ascii=False),
            "heartbeat_at": row.get("heartbeat_at"),
            "executor_pod": row.get("executor_pod"),
            "resume_count": int(row.get("resume_count") or 0),
        },
    )
