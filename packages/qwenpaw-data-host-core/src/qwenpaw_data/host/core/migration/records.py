"""Strict legacy metadata → SQL row values, without opening a database.

These are conversion primitives, not an importer. The caller must first verify
the source inventory, references and backup, and then insert all rows/files in
the migration transaction. Cross-record ownership and collisions are not proved
by converting one record. Non-representable states fail instead of being erased.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from qwenpaw_data.host.core.api.models.artifact import (
    ArtifactCommentSchema,
    ArtifactLineRefSchema,
)
from qwenpaw_data.host.core.api.models.chat import ChatErrorSchema
from qwenpaw_data.host.core.api.models.cron import ScheduleSpec

from .legacy_json import MigrationError, _canonical, _component, identity_mapping

_SQL_INT_MAX = 2**31 - 1


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _Identity(_Source):
    user_id: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class _Owned(_Source):
    id: str
    identity: _Identity
    created_at: AwareDatetime


class _Session(_Owned):
    agent_id: str = "default"
    title: str = ""
    datasource_id: str | None = None
    chat_count: int = Field(default=0, ge=0, le=_SQL_INT_MAX)
    channel: str = "console"
    updated_at: AwareDatetime
    deleted_at: AwareDatetime | None = None
    parent_session_id: str | None = None
    forked_from_chat_id: str | None = None


class _AttachmentRef(_Source):
    attachment_id: str
    filename: str


class _Chat(_Owned):
    session_id: str
    sequence: int = Field(ge=1, le=_SQL_INT_MAX)
    user_input: str
    datasource_id: str | None = None
    kind: Literal["simple", "planned"] = "simple"
    status: Literal["created", "running", "completed", "failed", "canceled"]
    last_sequence_number: int = Field(default=-1, ge=-1, le=_SQL_INT_MAX)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    active_duration_ms: int = Field(default=0, ge=0, le=_SQL_INT_MAX)
    error: ChatErrorSchema | None = None
    plan: dict[str, Any] | None = None
    artifact_comments: list[ArtifactCommentSchema] = Field(default_factory=list)
    attachments: list[_AttachmentRef] = Field(default_factory=list)
    dataset_names: list[str] = Field(default_factory=list)
    updated_at: AwareDatetime


class _Attachment(_Owned):
    session_id: str
    filename: str
    storage_path: str


class _UserOwned(_Source):
    id: str
    user_id: str
    created_at: AwareDatetime


class _Settlement(_UserOwned):
    session_id: str
    source_chat_id: str
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "queried", "confirmed", "dismissed"]
    updated_at: AwareDatetime
    confirmed_at: AwareDatetime | None = None


class _Feedback(_UserOwned):
    session_id: str
    chat_id: str
    kind: Literal["like", "dislike", "copy", "artifact_comment"]
    reason: str | None = None
    detail: str | None = None
    artifact_ref: ArtifactLineRefSchema | None = None


class _Schedule(_Source):
    type: Literal["cron", "once"] = "cron"
    cron: str | None = None
    run_at: AwareDatetime | None = None
    timezone: str = "Asia/Shanghai"


class _Cron(_UserOwned):
    name: str
    enabled: bool
    message: str
    datasource_id: str
    session_id: str | None = None
    schedule: _Schedule
    updated_at: AwareDatetime


_MODELS = {
    "sessions": (_Session, "sessions"),
    "chats": (_Chat, "chats"),
    "attachments": (_Attachment, "attachments"),
    "settlement": (_Settlement, "settlement_cards"),
    "feedback": (_Feedback, "feedbacks"),
    "cron": (_Cron, "cron_jobs"),
}


@dataclass(frozen=True)
class PreparedRecord:
    table: str
    values: dict[str, Any] = field(repr=False)
    # These describe explicit conversion decisions; never include payload data.
    notices: tuple[str, ...] = ()


def convert_record(
    kind: str,
    document: Any,
    *,
    users: dict[str, dict[str, str]],
    disable_cron: bool = False,
) -> PreparedRecord:
    """Prepare one supported metadata record; never commit or schedule it.

    Enabled cron requires explicit permission to disable on import. Existing
    running/created chats and nonempty plans require separate reconciliation;
    they must not be imported as completed, automatically resumed or discarded.
    Identity attrs remain in the mandatory original backup, not in current Auth.
    """
    if kind not in _MODELS:
        raise MigrationError("Unsupported legacy metadata kind")
    if type(disable_cron) is not bool:
        raise MigrationError("Cron disabling must be an explicit boolean")
    users = identity_mapping({"version": 1, "users": users})
    model, table = _MODELS[kind]
    try:
        # JSON strict mode accepts ISO timestamps but never coerces bools or
        # numeric strings into integers. Redact all pydantic input in errors.
        source = model.model_validate_json(_canonical(document), strict=True)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MigrationError(f"Invalid or unsupported legacy {kind} record") from None
    values = source.model_dump()
    identity = values.pop("identity", None)
    who = identity["user_id"] if identity is not None else values.pop("user_id")
    if who not in users:
        raise MigrationError("Record requires an explicit identity mapping")
    notices = []
    if identity is not None and identity["attrs"]:
        notices.append("legacy_identity_attrs_retained_in_source_backup")
    values.update(users[who])
    for key in (
        "id",
        "agent_id",
        "session_id",
        "parent_session_id",
        "forked_from_chat_id",
        "chat_id",
        "source_chat_id",
    ):
        if values.get(key) is not None:
            _component(values[key])
    created = values["created_at"]
    if values.get("updated_at", created) < created:
        raise MigrationError("Legacy update timestamp precedes creation")
    if kind == "sessions":
        values.update(share_scope="none", enable_clarification=True)
    elif kind == "chats":
        if values["status"] in {"created", "running"}:
            raise MigrationError(
                "Active legacy chat requires explicit interruption reconciliation"
            )
        if values["completed_at"] is None:
            raise MigrationError(
                "Terminal legacy chat requires its completion timestamp"
            )
        if values["completed_at"] < (values["started_at"] or created):
            raise MigrationError("Legacy completion timestamp precedes start")
        if values["plan"] not in (None, {}, {"tasks": []}):
            raise MigrationError(
                "Historical plan requires explicit lossless disposition"
            )
        values["plan"] = {"tasks": []}
        for ref in values["attachments"]:
            _component(ref["attachment_id"])
            _component(ref["filename"])
        for old, new in (
            ("error", "error_json"),
            ("artifact_comments", "artifact_comments_json"),
            ("attachments", "attachments_json"),
        ):
            values[new] = values.pop(old)
        values.update(
            agent_state_json=None,
            enable_clarification=True,
            heartbeat_at=None,
            otel_span_id=None,
        )
    elif kind == "attachments":
        _component(values["filename"])
        expected = f"uploads/{values['session_id']}/{values['filename']}"
        if values["storage_path"] != expected:
            raise MigrationError(
                "Attachment storage path disagrees with its session and filename"
            )
        values["attachment_id"] = values.pop("id")
    elif kind == "settlement":
        values["fields_json"] = values.pop("fields")
    elif kind == "feedback":
        values["artifact_ref_json"] = values.pop("artifact_ref")
    elif kind == "cron":
        if values["enabled"]:
            if not disable_cron:
                raise MigrationError(
                    "Enabled legacy cron requires explicit disable-on-import consent"
                )
            values["enabled"] = False
            notices.append("cron_disabled_on_import")
        schedule = source.schedule
        # Reject contradictory fields before the service normalizer can discard
        # them. Match its day-of-week semantics, but never calculate a due run.
        if (
            schedule.type == "cron"
            and (
                not schedule.cron
                or len(schedule.cron.split()) != 5
                or schedule.run_at is not None
            )
        ) or (
            schedule.type == "once"
            and (schedule.run_at is None or schedule.cron is not None)
        ):
            raise MigrationError("Legacy cron schedule requires reconciliation")
        try:
            normalized = ScheduleSpec.model_validate_json(schedule.model_dump_json())
            timezone = ZoneInfo(normalized.timezone)
            if normalized.type == "cron":
                CronTrigger.from_crontab(normalized.cron, timezone=timezone)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise MigrationError("Invalid legacy cron expression or timezone") from None
        values.pop("schedule")
        values["schedule_json"] = normalized.model_dump(mode="json")
        if values["schedule_json"] != schedule.model_dump(mode="json"):
            notices.append("cron_schedule_normalized")
        values.update(
            channel="console",
            session_policy="reuse",
            target_external_key=None,
            next_run_at=None,
            running_at=None,
            heartbeat_at=None,
            source_chat_id=None,
        )
    return PreparedRecord(table, values, tuple(notices))
