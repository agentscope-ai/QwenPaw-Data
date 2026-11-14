"""Persist task plans and coordinate executions through shared SQL."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from ..db import get_database
from ..schema.relational import cron_jobs as jobs
from .schedule import ScheduleSpec


class CronJobRepository:
    """Store schedules and atomically claim, renew, and release executions."""

    @property
    def _engine(self) -> Engine:
        return get_database().engine

    def create(
        self,
        job_id: str,
        handler: str,
        schedule: ScheduleSpec,
        *,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> bool:
        """Insert a task once, preserving an existing plan across restarts."""
        now = now or datetime.now(UTC)
        insert = sqlite_insert if self._engine.dialect.name == "sqlite" else pg_insert
        statement = (
            insert(jobs)
            .values(
                id=job_id,
                handler=handler,
                enabled=enabled,
                schedule_json=schedule.model_dump_json(),
                next_run_at=self._stamp(schedule.next_run(now)) if enabled else None,
                created_at=self._stamp(now),
                updated_at=self._stamp(now),
            )
            .on_conflict_do_nothing(index_elements=[jobs.c.id])
            .returning(jobs.c.id)
        )
        with self._engine.begin() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def get(self, job_id: str) -> dict | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(jobs).where(jobs.c.id == job_id))
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def update(
        self,
        job_id: str,
        schedule: ScheduleSpec,
        *,
        enabled: bool = True,
    ) -> None:
        """Replace a task's timing without taking over an active execution."""
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    enabled=enabled,
                    schedule_json=schedule.model_dump_json(),
                    next_run_at=self._stamp(schedule.next_run(now))
                    if enabled
                    else None,
                    run_requested=False,
                    updated_at=self._stamp(now),
                )
            )

    def run_now(self, job_id: str) -> None:
        """Request one execution after any active run; repeated requests coalesce."""
        now = self._stamp(datetime.now(UTC))
        with self._engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    next_run_at=now,
                    run_requested=True,
                    updated_at=now,
                )
            )

    def delete(self, job_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(delete(jobs).where(jobs.c.id == job_id))

    def list_scheduled(self, handlers: tuple[str, ...]) -> list[dict]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(jobs)
                    .where(
                        jobs.c.handler.in_(handlers),
                        jobs.c.next_run_at.is_not(None),
                    )
                    .order_by(jobs.c.next_run_at, jobs.c.id)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def try_start(self, job_id: str, now: datetime) -> dict | None:
        """Advance a due plan. One row updated means this process runs it."""
        row = self.get(job_id)
        stamp = self._stamp(now)
        if row is None or row["next_run_at"] is None:
            return None
        schedule = ScheduleSpec.model_validate_json(row["schedule_json"])
        following = self._stamp(schedule.next_run(now)) if row["enabled"] else None
        with self._engine.begin() as connection:
            result = connection.execute(
                update(jobs)
                .where(
                    jobs.c.id == job_id,
                    jobs.c.next_run_at == row["next_run_at"],
                    jobs.c.next_run_at <= stamp,
                    jobs.c.schedule_json == row["schedule_json"],
                    jobs.c.enabled == row["enabled"],
                    jobs.c.run_requested == row["run_requested"],
                )
                .values(
                    next_run_at=following,
                    run_requested=False,
                    updated_at=stamp,
                )
            )
        if result.rowcount != 1:
            return None
        return {**row, "started_at": stamp}

    @staticmethod
    def _stamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("cron timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
