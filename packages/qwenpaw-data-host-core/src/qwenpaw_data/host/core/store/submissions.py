# -*- coding: utf-8 -*-
"""Durable submission receipts for trusted backend callers.

Acceptance is atomic with session/run creation. Only the insert winner may
schedule execution; a receipt is never discarded or reused after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.api.models.stream_objects import (
    dump_stream_object,
    parse_stream_object,
)
from qwenpaw_data.host.core.db.tables import (
    ChatEventRow,
    ChatRow,
    SubmissionCommandRow,
    SubmissionRow,
    SessionRow,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.store.sql_store import (
    _chat_to_row,
    _session_to_row,
    _session_from_row,
    _apply_session,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow

_TERMINAL = ("completed", "failed", "cancelled")
_RESTART_REASON = "executor_restarted"


@dataclass(frozen=True)
class SubmissionRecord:
    user_id: str
    submission_id: str
    request_digest: str
    session_id: str
    run_id: str


@dataclass(frozen=True)
class SubmissionCommandRecord:
    user_id: str
    submission_id: str
    command_id: str
    kind: str
    request_digest: str
    state: str
    reason: str | None


def _record(row: SubmissionRow) -> SubmissionRecord:
    return SubmissionRecord(
        row.user_id, row.submission_id, row.request_digest, row.session_id, row.run_id
    )


def _command_record(row: SubmissionCommandRow) -> SubmissionCommandRecord:
    return SubmissionCommandRecord(
        row.user_id,
        row.submission_id,
        row.command_id,
        row.kind,
        row.request_digest,
        "unknown" if row.state == "prepared" else row.state,
        row.reason,
    )


def _terminal_events(chat_id):
    return select(ChatEventRow).where(
        ChatEventRow.chat_id == chat_id,
        ChatEventRow.object == "response",
        ChatEventRow.payload["status"].as_string().in_(_TERMINAL),
    )


class SQLSubmissionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def lookup(self, user_id: str, submission_id: str) -> SubmissionRecord | None:
        async with self._sessions() as db:
            row = await db.get(SubmissionRow, (user_id, submission_id))
            return _record(row) if row is not None else None

    async def lookup_command(
        self,
        user_id: str,
        submission_id: str,
        command_id: str,
    ) -> SubmissionCommandRecord | None:
        async with self._sessions() as db:
            row = await db.get(
                SubmissionCommandRow,
                (user_id, submission_id, command_id),
            )
            return _command_record(row) if row is not None else None

    async def prepare_command(
        self,
        *,
        record: SubmissionRecord,
        command_id: str,
        kind: str,
        request_digest: str,
    ) -> tuple[SubmissionCommandRecord, bool]:
        existing = await self.lookup_command(
            record.user_id,
            record.submission_id,
            command_id,
        )
        if existing is not None:
            if existing.kind != kind or existing.request_digest != request_digest:
                raise RuntimeError("CONFLICT: command_id was used for different inputs")
            return existing, False
        row = SubmissionCommandRow(
            user_id=record.user_id,
            submission_id=record.submission_id,
            command_id=command_id,
            kind=kind,
            request_digest=request_digest,
            state="prepared",
        )
        try:
            async with self._sessions() as db, db.begin():
                db.add(row)
                await db.flush()
        except IntegrityError:
            existing = await self.lookup_command(
                record.user_id,
                record.submission_id,
                command_id,
            )
            if existing is None:
                raise
            if existing.kind != kind or existing.request_digest != request_digest:
                raise RuntimeError(
                    "CONFLICT: command_id was used for different inputs"
                ) from None
            return existing, False
        return _command_record(row), True

    async def finish_command(
        self,
        record: SubmissionCommandRecord,
        *,
        state: str,
        reason: str | None = None,
    ) -> SubmissionCommandRecord:
        if state not in {"accepted", "rejected"}:
            raise ValueError("invalid command outcome")
        async with self._sessions() as db, db.begin():
            row = await db.get(
                SubmissionCommandRow,
                (record.user_id, record.submission_id, record.command_id),
            )
            if row is None:
                raise LookupError("submission command not found")
            if row.state != "prepared":
                return _command_record(row)
            row.state = state
            row.reason = reason
            row.updated_at = utcnow()
            await db.flush()
            return _command_record(row)

    @staticmethod
    def _check_digest(record: SubmissionRecord, digest: str) -> None:
        if record.request_digest != digest:
            raise RuntimeError("CONFLICT: submission_id was used for different inputs")

    async def submit(
        self,
        *,
        identity: Identity,
        submission_id: str,
        request_digest: str,
        text: str,
        datasource_id: str | None,
        agent_id: str,
        session_id: str | None = None,
        dataset_names: list[str] | None = None,
        attachments: list[dict] | None = None,
        artifact_comments: list[dict] | None = None,
    ) -> tuple[SubmissionRecord, bool]:
        existing = await self.lookup(identity.user_id, submission_id)
        if existing is not None:
            self._check_digest(existing, request_digest)
            return existing, False

        row = SubmissionRow(
            user_id=identity.user_id,
            submission_id=submission_id,
            request_digest=request_digest,
            session_id=session_id or create_id("ses"),
            run_id=create_id("chat"),
        )
        try:
            async with self._sessions() as db, db.begin():
                db.add(row)
                # The DB unique key arbitrates concurrent submissions. Do not
                # call runtime/model code until all three rows have committed.
                await db.flush()
                session_row = None
                if session_id is not None:
                    session_row = await db.scalar(
                        select(SessionRow)
                        .where(
                            SessionRow.id == session_id,
                            SessionRow.user_id == identity.user_id,
                            SessionRow.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if session_row is None:
                        raise LookupError("session not found")
                    session = _session_from_row(session_row)
                else:
                    session = Session.create(
                        identity=identity,
                        agent_id=agent_id,
                        datasource_id=datasource_id,
                        channel="pawapp",
                    )
                    session.id = row.session_id
                active = await db.scalar(
                    select(
                        exists().where(
                            ChatRow.session_id == session.id,
                            ChatRow.status == "running",
                        )
                    )
                )
                chat = session.open_chat(
                    text=text,
                    datasource_id=datasource_id,
                    has_active_chat=bool(active),
                    dataset_names=dataset_names,
                    attachments=attachments,
                    artifact_comments=artifact_comments,
                )
                chat.id = row.run_id
                if session_row is not None:
                    _apply_session(session_row, session)
                else:
                    db.add(_session_to_row(session))
                db.add(_chat_to_row(chat))
        except IntegrityError:
            existing = await self.lookup(identity.user_id, submission_id)
            if existing is None:
                raise  # Some other integrity violation; not a deduplication hit.
            self._check_digest(existing, request_digest)
            return existing, False
        return _record(row), True

    async def run_snapshot(self, record: SubmissionRecord) -> dict[str, Any]:
        async with self._sessions() as db:
            chat = await db.get(ChatRow, record.run_id)
            if (
                chat is None
                or chat.session_id != record.session_id
                or chat.user_id != record.user_id
            ):
                return {"status": "unknown", "reason": "run_missing"}
            terminal = await db.scalar(
                _terminal_events(chat.id)
                .order_by(ChatEventRow.sequence_number.desc())
                .limit(1)
            )
            status = "running" if chat.status == "running" else "reconciling"
            error = chat.error_json
            if terminal is not None:
                status = {
                    "completed": "succeeded",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }[terminal.payload["status"]]
                error = terminal.payload.get("error")
                if ((error or {}).get("details") or {}).get(
                    "reason"
                ) == _RESTART_REASON:
                    status = "interrupted"
            return {
                "status": status,
                "last_sequence_number": max(
                    chat.last_sequence_number,
                    terminal.sequence_number if terminal is not None else -1,
                ),
                "error": error,
            }

    async def recover(self) -> None:
        """Startup only, before any writers: persist an explicit terminal fact.

        A terminal response is the success boundary. A saved completed chat
        without that response may have lost in-memory output finalization, so
        it is interrupted, never synthesized as a successful execution.
        """
        async with self._sessions() as db, db.begin():
            chats = (
                await db.scalars(
                    select(ChatRow)
                    .join(SubmissionRow, SubmissionRow.run_id == ChatRow.id)
                    .where(
                        or_(
                            ChatRow.status == "running",
                            ~exists(_terminal_events(ChatRow.id)),
                        )
                    )
                )
            ).all()
            for chat in chats:
                terminal = await db.scalar(
                    _terminal_events(chat.id)
                    .order_by(ChatEventRow.sequence_number.desc())
                    .limit(1)
                )
                now = utcnow()
                if terminal is not None:
                    chat.status = {
                        "completed": "completed",
                        "failed": "failed",
                        "cancelled": "canceled",
                    }[terminal.payload["status"]]
                    chat.error_json = terminal.payload.get("error")
                else:
                    if chat.status not in ("failed", "canceled"):
                        chat.error_json = {
                            "code": "VALIDATION",
                            "message": "Engine restarted before a terminal response was persisted",
                            "details": {
                                "reason": _RESTART_REASON,
                                "previous_status": chat.status,
                            },
                        }
                        chat.status = "canceled"
                    chat.last_sequence_number += 1
                    payload = {
                        "object": "response",
                        "id": create_id("response"),
                        "status": "failed" if chat.status == "failed" else "cancelled",
                        "session_id": chat.session_id,
                        "chat_id": chat.id,
                        "sequence_number": chat.last_sequence_number,
                        "error": chat.error_json,
                    }
                    db.add(
                        ChatEventRow(
                            chat_id=chat.id,
                            session_id=chat.session_id,
                            user_id=chat.user_id,
                            sequence_number=chat.last_sequence_number,
                            object="response",
                            payload=dump_stream_object(parse_stream_object(payload)),
                        )
                    )
                chat.completed_at = chat.completed_at or now
                chat.updated_at = now
