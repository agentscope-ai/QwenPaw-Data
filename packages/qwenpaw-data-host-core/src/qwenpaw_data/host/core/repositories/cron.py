# -*- coding: utf-8 -*-
"""Cron job persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.models.cron import CronJobWrite, ScheduleSpec
from qwenpaw_data.host.core.cron.files import JobFiles
from qwenpaw_data.host.core.db.tables import ChatRow, CronJobRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import tracer_record_behavior
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.cron import compute_next_run_at
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


def _to_dict(row: CronJobRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "tenant_id": row.tenant_id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "enabled": row.enabled,
        "message": row.message,
        "datasource_id": row.datasource_id,
        "channel": row.channel,
        "session_policy": row.session_policy,
        "session_id": row.session_id,
        "target_external_key": row.target_external_key,
        "attachments": JobFiles(
            Identity(
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
            ),
            row.id,
        ).filenames(),
        "source_chat_id": row.source_chat_id,
        "schedule": ScheduleSpec.model_validate(row.schedule_json).model_dump(
            mode="json"
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _next_run_at(schedule: ScheduleSpec, enabled: bool) -> datetime | None:
    return compute_next_run_at(schedule) if enabled else None


class CronJobRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def list(
        self,
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        q = (
            select(CronJobRow)
            .where(
                CronJobRow.user_id == self.identity.user_id,
                CronJobRow.tenant_id == self.identity.tenant_id,
                CronJobRow.workspace_id == self.identity.workspace_id,
            )
            .order_by(CronJobRow.created_at.desc())
        )

        if keyword:
            q = q.where(
                or_(
                    CronJobRow.name.contains(keyword),
                    CronJobRow.message.contains(keyword),
                )
            )
        if enabled is not None:
            q = q.where(CronJobRow.enabled == enabled)
        if channel:
            q = q.where(CronJobRow.channel == channel)
        rows = (await self.db.scalars(q)).all()
        return [_to_dict(r) for r in rows]

    async def get(self, job_id: str) -> dict[str, Any]:
        return _to_dict(await self._get_row(job_id))

    async def create(self, body: CronJobWrite) -> dict[str, Any]:
        now = utcnow()
        row = CronJobRow(
            id=create_id("cron"),
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
            workspace_id=self.identity.workspace_id,
            name=body.name,
            enabled=body.enabled,
            message=body.message,
            datasource_id=body.datasource_id,
            channel=body.channel,
            session_policy=body.session_policy,
            session_id=None,
            target_external_key=body.target_external_key,
            source_chat_id=body.source_chat_id,
            schedule_json=body.schedule.model_dump(mode="json"),
            next_run_at=_next_run_at(body.schedule, body.enabled),
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        await self.db.flush()
        if body.source_chat_id:
            chat = await self.db.get(ChatRow, body.source_chat_id)
            if chat is not None:
                await tracer_record_behavior(
                    self.db,
                    self.identity,
                    session_id=chat.session_id,
                    chat_id=body.source_chat_id,
                    kind="schedule_saved",
                    target_type="cron",
                    target_id=row.id,
                    payload={"name": row.name},
                    score_name="schedule_saved",
                    comment=row.name,
                )
        return _to_dict(row)

    async def replace(self, job_id: str, body: CronJobWrite) -> dict[str, Any]:
        row = await self._get_row(job_id)
        if row.datasource_id != body.datasource_id or row.channel != body.channel:
            row.session_id = None
        row.name = body.name
        row.enabled = body.enabled
        row.message = body.message
        row.datasource_id = body.datasource_id
        row.channel = body.channel
        row.session_policy = body.session_policy
        row.target_external_key = body.target_external_key
        row.schedule_json = body.schedule.model_dump(mode="json")
        row.next_run_at = _next_run_at(body.schedule, body.enabled)
        row.updated_at = utcnow()
        await self.db.flush()
        return _to_dict(row)

    async def set_enabled(self, job_id: str, enabled: bool) -> dict[str, Any]:
        row = await self._get_row(job_id)
        row.enabled = enabled
        if enabled:
            # Resume from now so slots missed while paused are not replayed.
            row.next_run_at = compute_next_run_at(
                ScheduleSpec.model_validate(row.schedule_json)
            )
        else:
            row.next_run_at = None
        row.updated_at = utcnow()
        await self.db.flush()
        return _to_dict(row)

    async def run_now(self, job_id: str) -> dict[str, Any]:
        row = await self._get_row(job_id)
        row.next_run_at = utcnow()
        row.updated_at = utcnow()
        await self.db.flush()
        return _to_dict(row)

    async def delete(self, job_id: str) -> None:
        await self.db.delete(await self._get_row(job_id))
        await self.db.flush()
        JobFiles(self.identity, job_id).clear()

    async def _get_row(self, job_id: str) -> CronJobRow:
        row = await self.db.get(CronJobRow, job_id)
        if (
            row is None
            or row.user_id != self.identity.user_id
            or row.tenant_id != self.identity.tenant_id
            or row.workspace_id != self.identity.workspace_id
        ):
            raise LookupError("cron job not found")
        return row

    @staticmethod
    async def list_ready_to_run(db: AsyncSession) -> list[dict[str, Any]]:
        now = utcnow()
        rows = (
            await db.scalars(
                select(CronJobRow).where(
                    CronJobRow.next_run_at <= now,
                    CronJobRow.running_at.is_(None),
                )
            )
        ).all()
        return [_to_dict(r) for r in rows]

    @staticmethod
    async def try_start(db: AsyncSession, job_id: str) -> datetime | None:
        now = utcnow()
        row = await db.get(CronJobRow, job_id)
        if row is None or row.next_run_at is None:
            return None
        new_next = (
            compute_next_run_at(ScheduleSpec.model_validate(row.schedule_json), now=now)
            if row.enabled
            else None
        )
        result = await db.execute(
            update(CronJobRow)
            .where(
                CronJobRow.id == job_id,
                CronJobRow.next_run_at <= now,
                CronJobRow.running_at.is_(None),
            )
            .values(
                next_run_at=new_next,
                running_at=now,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return now

    @staticmethod
    async def clear_expired(db: AsyncSession) -> None:
        now = utcnow()
        cutoff = now - timedelta(seconds=get_settings().cron.running_ttl_seconds)
        stale = or_(
            CronJobRow.heartbeat_at.is_(None),
            CronJobRow.heartbeat_at < cutoff,
        )
        rows = (
            await db.scalars(
                select(CronJobRow).where(
                    CronJobRow.running_at.is_not(None),
                    stale,
                )
            )
        ).all()
        for row in rows:
            started = row.running_at
            if started is None:
                continue
            new_next = row.next_run_at
            if row.enabled and row.next_run_at is not None and row.next_run_at <= now:
                new_next = compute_next_run_at(
                    ScheduleSpec.model_validate(row.schedule_json), now=now
                )
            await db.execute(
                update(CronJobRow)
                .where(
                    CronJobRow.id == row.id,
                    CronJobRow.running_at == started,
                    stale,
                )
                .values(
                    running_at=None,
                    heartbeat_at=None,
                    next_run_at=new_next,
                    updated_at=now,
                )
            )

    @staticmethod
    async def clear_running(
        db: AsyncSession, job_id: str, running_at: datetime
    ) -> None:
        now = utcnow()
        row = await db.get(CronJobRow, job_id)
        if row is None or row.running_at != running_at:
            return
        started = row.running_at
        row.running_at = None
        row.heartbeat_at = None
        # next_run_at is the following fire, written when this run started.
        # If this run lasted past that time, jump ahead so we do not fire again on finish.
        if (
            row.enabled
            and started is not None
            and row.next_run_at is not None
            and started < row.next_run_at <= now
        ):
            row.next_run_at = compute_next_run_at(
                ScheduleSpec.model_validate(row.schedule_json), now=now
            )
        row.updated_at = now

    @staticmethod
    async def fill_missing_next_run_at(db: AsyncSession) -> None:
        now = utcnow()
        rows = (await db.scalars(select(CronJobRow))).all()
        for row in rows:
            if not row.enabled:
                row.next_run_at = None
            elif row.next_run_at is None:
                row.next_run_at = compute_next_run_at(
                    ScheduleSpec.model_validate(row.schedule_json), now=now
                )
