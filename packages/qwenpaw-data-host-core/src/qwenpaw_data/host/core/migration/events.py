"""Validate historical event streams and rebuild their SQL read projections.

No event is broadcast, rescheduled or rewritten. Unsupported legacy events (in
particular task_status from the retired DAG) require a separate archival policy.
The complete original source remains mandatory in the verified backup.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject

from .legacy_json import MigrationError, _canonical, _component
from .records import PreparedRecord, _SQL_INT_MAX


_EVENT = TypeAdapter(StreamObject)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MigrationError("Historical event requires an aware timestamp")
    return value


def convert_events(events: Any, *, chat: PreparedRecord) -> tuple[PreparedRecord, ...]:
    """Prepare event history and latest projections for an already converted chat.

    This does not reconcile missing events or a lagging chat watermark. Such
    changes require an explicit migration decision, not an implicit repair.
    Cross-chat/global artifact ID conflicts must be checked by the importer.
    """
    if chat.table != "chats" or not isinstance(events, list):
        raise MigrationError("Event conversion requires a prepared chat and event list")
    cv = chat.values
    cid, sid = _component(cv["id"]), _component(cv["session_id"])
    if len(events) > _SQL_INT_MAX or cv["last_sequence_number"] != len(events) - 1:
        raise MigrationError(
            "Chat watermark and event log require explicit reconciliation"
        )
    identity = {key: cv[key] for key in ("user_id", "tenant_id", "workspace_id")}
    rows, projections = [], {}
    terminal_status = None

    def project(table, key, values):
        previous = projections.get((table, key))
        # Upsert projections keep their original creation time, like repositories.
        if previous is not None and table != "artifacts":
            values["created_at"] = previous.values["created_at"]
            if table == "biz_events":
                values["seq"] = previous.values["seq"]
        projections[table, key] = PreparedRecord(table, {**identity, **values})

    for sequence, payload in enumerate(events):
        try:
            event = _EVENT.validate_json(_canonical(payload), strict=True)
        except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
            raise MigrationError(
                "Invalid or unsupported historical event; explicit disposition required"
            ) from None
        if (
            event.chat_id != cid
            or event.session_id != sid
            or event.sequence_number != sequence
        ):
            raise MigrationError("Historical event identity or dense sequence mismatch")
        created = _aware(event.created_at)
        rows.append(
            PreparedRecord(
                "chat_events",
                {
                    **identity,
                    "chat_id": cid,
                    "session_id": sid,
                    "sequence_number": sequence,
                    "object": event.object,
                    "payload": deepcopy(payload),
                    "created_at": created,
                },
            )
        )
        if event.object == "response":
            _component(event.id)
            terminal = {
                "completed": "completed",
                "failed": "failed",
                "cancelled": "canceled",
            }.get(event.status)
            if terminal_status is not None:
                raise MigrationError(
                    "Historical stream contains a response after its terminal response"
                )
            if terminal:
                terminal_status = terminal
        elif event.object == "segment":
            segment = event.segment
            if segment.chat_id not in (None, cid):
                raise MigrationError("Historical segment refers to another chat")
            if (
                not 0
                <= segment.coverage.start_seq
                <= segment.coverage.end_seq
                < len(events)
            ):
                raise MigrationError(
                    "Historical segment coverage is outside the event log"
                )
            data = segment.model_dump(mode="json")
            project(
                "segments",
                _component(segment.segment_id),
                {
                    "id": segment.segment_id,
                    "chat_id": cid,
                    "title": segment.title,
                    "input": segment.input,
                    "behavior": segment.behavior,
                    "conclusion": segment.conclusion,
                    "artifact_json": data["artifact"],
                    "confidence_json": data["confidence"],
                    "start_seq": segment.coverage.start_seq,
                    "end_seq": segment.coverage.end_seq,
                    "started_at": segment.started_at,
                    "ended_at": segment.ended_at,
                    "created_at": created,
                },
            )
        elif event.object == "biz_event":
            item = event.biz_event
            if item.chat_id not in (None, cid) or not 0 <= item.seq <= _SQL_INT_MAX:
                raise MigrationError(
                    "Historical business event has invalid ownership or order"
                )
            project(
                "biz_events",
                _component(item.event_id),
                {
                    "event_id": item.event_id,
                    "chat_id": cid,
                    "seq": item.seq,
                    "channel": item.channel,
                    "block_id": item.block_id,
                    "status": item.status,
                    "presentation_json": item.model_dump(mode="json")["presentation"],
                    "started_at": item.started_at,
                    "ended_at": item.ended_at,
                    "created_at": created,
                },
            )
        elif event.object == "artifact.registered":
            item = event.artifact
            if item.session_id != sid or item.chat_id not in (None, cid):
                raise MigrationError(
                    "Historical artifact refers to another chat or session"
                )
            if _aware(item.updated_at) < _aware(item.created_at):
                raise MigrationError("Historical artifact update precedes creation")
            # Workspace-relative path; source file/ownership verification and
            # placement are separate and must not be inferred from this event.
            if not item.path or any(
                part in {"", ".", ".."} for part in item.path.split("/")
            ):
                raise MigrationError(
                    "Historical artifact requires a safe relative path"
                )
            for part in item.path.split("/"):
                _component(part)
            project(
                "artifacts", _component(item.id), {**item.model_dump(), "chat_id": cid}
            )
        elif event.object == "followup.generated":
            if event.followup.chat_id not in (None, cid):
                raise MigrationError("Historical followup refers to another chat")
            project(
                "followups",
                cid,
                {
                    "chat_id": cid,
                    "questions_json": list(event.followup.questions),
                    "created_at": created,
                },
            )
    if terminal_status != cv["status"]:
        raise MigrationError(
            "Historical terminal response and chat status require reconciliation"
        )
    return tuple([*rows, *projections.values()])
