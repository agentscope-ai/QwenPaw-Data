"""Persist embedding jobs and coordinate workspace rebuild attempts."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from sqlalchemy import and_, case, exists, insert, or_, select, update

from context_manager.heartbeat import is_fresh, lock_scope_sync, stale_cutoff
from context_manager.utils.pod import current_pod_name

from ..db import get_database
from ..schema.relational import embedding_jobs as jobs
from ..schema.relational import recovery_runs as runs

JobOwner = tuple[str, str]  # tenant, workspace
_ACTIVE = ("pending", "running")


def rebuild_rid(job_id, attempted_at):
    """Derive the durable attempt identity from existing job columns."""
    return "embedding:" + job_id + ":" + attempted_at


class RebuildBusy(RuntimeError):
    """Another rebuild is already active in this tenant/workspace."""

    def __init__(self, job_id: str) -> None:
        super().__init__("an embedding rebuild is already active")
        self.job_id = job_id


class EmbeddingJobRepository:
    """Borrow the shared engine for job records and atomic execution claims."""

    @staticmethod
    def _owner(table, owner):
        return (table.c.tenant_id == owner[0], table.c.workspace_id == owner[1])

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = {
            **row,
            "progress": json.loads(row["progress"]),
            "config_snapshot": json.loads(row["config_snapshot"]),
        }
        recovery_state = result.pop("_recovery_state", None)
        result_status = result.pop("_result_status", None)
        result.pop("_recovery_message", None)
        if recovery_state == "CLOSED":
            success = result_status in ("SUCCESS", "DEGRADED")
            result.update(
                status="success" if success else "failed",
                error=None
                if success
                else (
                    "Embedding rebuild was rolled back; "
                    "retry to rebuild from the beginning."
                ),
                progress={
                    **result["progress"],
                    "phase": "done" if success else "failed",
                },
            )
            return result
        if recovery_state is not None and result["status"] in _ACTIVE:
            result.update(
                status="running",
                error=None,
                progress={**result["progress"], "phase": "recovering"},
            )
        return result

    @staticmethod
    def _read_statement():
        return select(
            jobs,
            runs.c.recovery_state.label("_recovery_state"),
            runs.c.result_status.label("_result_status"),
            runs.c.message.label("_recovery_message"),
        ).outerjoin(
            runs,
            and_(
                runs.c.tenant_id == jobs.c.tenant_id,
                runs.c.workspace_id == jobs.c.workspace_id,
                runs.c.task_type == "embedding",
                runs.c.task_id == jobs.c.job_id,
                runs.c.rid == rebuild_rid(jobs.c.job_id, jobs.c.attempted_at),
            ),
        )

    @staticmethod
    def _recovery_still_open():
        return exists(
            select(runs.c.rid).where(
                runs.c.tenant_id == jobs.c.tenant_id,
                runs.c.workspace_id == jobs.c.workspace_id,
                runs.c.task_type == "embedding",
                runs.c.task_id == jobs.c.job_id,
                runs.c.recovery_state != "CLOSED",
            )
        )

    def get(self, owner: JobOwner, job_id: str) -> dict | None:
        with get_database().engine.begin() as connection:
            row = (
                connection.execute(
                    self._read_statement().where(
                        *self._owner(jobs, owner), jobs.c.job_id == job_id
                    )
                )
                .mappings()
                .first()
            )
            return self._project_receipt(connection, row)

    def latest(self, owner: JobOwner) -> dict | None:
        with get_database().engine.begin() as connection:
            row = (
                connection.execute(
                    self._read_statement()
                    .where(*self._owner(jobs, owner))
                    .order_by(
                        case(
                            (
                                and_(
                                    jobs.c.status.in_(_ACTIVE),
                                    or_(
                                        runs.c.rid.is_(None),
                                        runs.c.recovery_state != "CLOSED",
                                    ),
                                ),
                                1,
                            ),
                            else_=0,
                        ).desc(),
                        jobs.c.attempted_at.desc(),
                        jobs.c.job_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return self._project_receipt(connection, row)

    def _project_receipt(self, connection, row):
        """Write a closed graph receipt onto a job that is still active."""
        result = self._decode(row)
        if (
            row is None
            or row.get("_recovery_state") != "CLOSED"
            or row["status"] not in _ACTIVE
        ):
            return result
        current = and_(
            *self._owner(jobs, (row["tenant_id"], row["workspace_id"])),
            jobs.c.job_id == row["job_id"],
            jobs.c.attempted_at == row["attempted_at"],
        )
        result["finished_at"] = datetime.now(UTC).isoformat()
        updated = connection.execute(
            update(jobs)
            .where(current, jobs.c.status.in_(_ACTIVE))
            .values(
                status=result["status"],
                error=result["error"],
                finished_at=result["finished_at"],
                progress=json.dumps(result["progress"], ensure_ascii=False),
            )
        )
        if updated.rowcount == 0:
            fresh = (
                connection.execute(
                    self._read_statement().where(
                        *self._owner(jobs, (row["tenant_id"], row["workspace_id"])),
                        jobs.c.job_id == row["job_id"],
                    )
                )
                .mappings()
                .one()
            )
            return self._decode(fresh)
        return result

    def reserve(self, job: dict, *, retry: bool) -> tuple[int, str | None]:
        """Insert one workspace rebuild, or reopen a failed job.

        Returns the attempt count and the recovery receipt this retry replaces.
        """
        owner = (job["tenant_id"], job["workspace_id"])
        now = time.time()
        cutoff = stale_cutoff(now)
        values = {
            **job,
            "progress": json.dumps(job["progress"], ensure_ascii=False),
            "config_snapshot": json.dumps(job["config_snapshot"], ensure_ascii=False),
            "heartbeat_at": now,
            "executor_pod": current_pod_name() or None,
            "resume_count": 0,
        }
        engine = get_database().engine
        with engine.begin() as connection:
            lock_scope_sync(connection, f"rebuild:{owner[0]}:{owner[1]}")
            active = (
                connection.execute(
                    select(jobs.c.job_id).where(
                        *self._owner(jobs, owner),
                        jobs.c.status.in_(_ACTIVE),
                    )
                )
                .scalars()
                .all()
            )
            seen = set(active)
            if retry:
                seen.add(job["job_id"])
            for task_id in seen:
                row = (
                    connection.execute(
                        self._read_statement().where(
                            *self._owner(jobs, owner), jobs.c.job_id == task_id
                        )
                    )
                    .mappings()
                    .first()
                )
                self._project_receipt(connection, row)
            active = (
                connection.execute(
                    select(jobs.c.job_id, jobs.c.heartbeat_at).where(
                        *self._owner(jobs, owner),
                        jobs.c.status.in_(_ACTIVE),
                    )
                )
                .mappings()
                .all()
            )
            fresh = [row for row in active if is_fresh(row["heartbeat_at"], now=now)]
            if fresh:
                raise RebuildBusy(str(fresh[0]["job_id"]))
            connection.execute(
                update(jobs)
                .where(
                    *self._owner(jobs, owner),
                    jobs.c.status.in_(_ACTIVE),
                    or_(
                        jobs.c.heartbeat_at.is_(None),
                        jobs.c.heartbeat_at < cutoff,
                    ),
                    ~self._recovery_still_open(),
                )
                .values(
                    status="failed",
                    error="执行中断",
                    finished_at=datetime.now(UTC).isoformat(),
                )
            )
            previous_rid = connection.execute(
                select(runs.c.rid).where(
                    *self._owner(runs, owner),
                    runs.c.task_type == "embedding",
                    runs.c.task_id == job["job_id"],
                )
            ).scalar_one_or_none()
            if retry:
                current = connection.execute(
                    select(jobs.c.resume_count).where(
                        *self._owner(jobs, owner),
                        jobs.c.job_id == job["job_id"],
                    )
                ).scalar_one()
                held = int(current)
                resume_count = 0 if held + 1 >= 3 else held + 1
                for name in (
                    "tenant_id",
                    "workspace_id",
                    "job_id",
                    "created_at",
                    "created_by",
                ):
                    del values[name]
                values["resume_count"] = resume_count
                result = connection.execute(
                    update(jobs)
                    .where(
                        *self._owner(jobs, owner),
                        jobs.c.job_id == job["job_id"],
                        jobs.c.status == "failed",
                    )
                    .values(**values)
                )
                if result.rowcount == 0:
                    raise ValueError("only failed embedding jobs can be retried")
                return resume_count, previous_rid
            connection.execute(insert(jobs).values(**values))
        return 0, previous_rid

    def heartbeat(self, owner: JobOwner, job_id: str, resume_count: int) -> bool:
        with get_database().engine.begin() as connection:
            result = connection.execute(
                update(jobs)
                .where(
                    *self._owner(jobs, owner),
                    jobs.c.job_id == job_id,
                    jobs.c.resume_count == resume_count,
                    jobs.c.status.in_(_ACTIVE),
                )
                .values(heartbeat_at=time.time())
            )
            return result.rowcount == 1

    def mark_running(self, owner: JobOwner, job_id: str, resume_count: int) -> bool:
        return self._update(
            owner,
            job_id,
            resume_count,
            {
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "error": None,
            },
        )

    def update_progress(
        self, owner: JobOwner, job_id: str, resume_count: int, progress: dict
    ) -> bool:
        return self._update(
            owner,
            job_id,
            resume_count,
            {"progress": json.dumps(progress, ensure_ascii=False)},
        )

    def finish(
        self,
        owner: JobOwner,
        job_id: str,
        resume_count: int,
        *,
        status: str,
        progress: dict,
        error: str | None = None,
    ) -> bool:
        return self._update(
            owner,
            job_id,
            resume_count,
            {
                "status": status,
                "error": error,
                "finished_at": datetime.now(UTC).isoformat(),
                "progress": json.dumps(progress, ensure_ascii=False),
            },
        )

    def claim_stale(self) -> list[dict]:
        """Take expired rebuilds that still have automatic attempts left."""
        now = time.time()
        cutoff = stale_cutoff(now)
        engine = get_database().engine
        open_recovery = self._recovery_still_open()
        stale = or_(jobs.c.heartbeat_at.is_(None), jobs.c.heartbeat_at < cutoff)
        with engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(
                    jobs.c.status.in_(_ACTIVE),
                    stale,
                    jobs.c.resume_count >= 3,
                    ~open_recovery,
                )
                .values(
                    status="failed",
                    error="执行中断",
                    finished_at=datetime.now(UTC).isoformat(),
                )
            )
            claimed = (
                connection.execute(
                    update(jobs)
                    .where(
                        jobs.c.status.in_(_ACTIVE),
                        stale,
                        jobs.c.resume_count < 3,
                        ~open_recovery,
                    )
                    .values(
                        heartbeat_at=now,
                        executor_pod=current_pod_name() or None,
                        resume_count=jobs.c.resume_count + 1,
                        status="pending",
                    )
                    .returning(jobs)
                )
                .mappings()
                .all()
            )
        return [self._decode(row) for row in claimed]

    def _update(
        self,
        owner: JobOwner,
        job_id: str,
        resume_count: int,
        changes: dict,
    ) -> bool:
        with get_database().engine.begin() as connection:
            if changes.get("status") not in (None, *_ACTIVE):
                row = (
                    connection.execute(
                        self._read_statement().where(
                            *self._owner(jobs, owner), jobs.c.job_id == job_id
                        )
                    )
                    .mappings()
                    .first()
                )
                state = None if row is None else row["_recovery_state"]
                if state == "CLOSED":
                    self._project_receipt(connection, row)
                    return True
                if state is not None:
                    # An open stack stays on the business row until its receipt closes.
                    return True
            result = connection.execute(
                update(jobs)
                .where(
                    *self._owner(jobs, owner),
                    jobs.c.job_id == job_id,
                    jobs.c.resume_count == resume_count,
                    jobs.c.status.in_(_ACTIVE),
                )
                .values(**changes)
            )
            return result.rowcount == 1
