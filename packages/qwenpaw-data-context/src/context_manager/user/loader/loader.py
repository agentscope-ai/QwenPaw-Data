"""Load independently versioned targets with complete earlier session context."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from itertools import chain, groupby, islice
from urllib.parse import quote

from ...utils.validation import require_trimmed_text
from .repository import ChatLoaderCheckpointRepository
from .contract import (
    ChatHistoryLoad,
    ChatHistoryMessage,
    UserChatBatch,
    UserHistoryScope,
)
from .database import (
    HistoryConnection,
    HistoryDataError,
    history_connection,
    parse_timestamp,
)
from .observations import odps_project_observations

USER_GRAPH_UPDATER_VERSION = "7"  # Reuse develop's accepted history checkpoints.
_SCOPE_FIELDS = ("tenant_id", "workspace_id", "user_id")


class ChatLoader:
    """Select changed message versions and acknowledge each committed target."""

    def __init__(
        self,
        checkpoints: ChatLoaderCheckpointRepository,
        *,
        updater_version: str = USER_GRAPH_UPDATER_VERSION,
    ) -> None:
        self._checkpoints = checkpoints
        self._updater_version = require_trimmed_text(updater_version, "updater_version")

    def load_pending(
        self,
        *,
        cutoff_at: datetime,
        target_scope: UserHistoryScope | None = None,
    ) -> ChatHistoryLoad:
        """Scan a read-only snapshot and report changed targets and scoped issues."""
        if not isinstance(cutoff_at, datetime) or cutoff_at.tzinfo is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        cutoff = cutoff_at.astimezone(UTC)
        sources: dict[UserHistoryScope, list[ChatHistoryMessage]] = defaultdict(list)
        issues = []
        with history_connection() as connection:
            rows = _history_rows(connection, cutoff, target_scope)
            for _, session_rows in groupby(rows, key=lambda row: row["session_id"]):
                first = next(session_rows)
                session_issues = []
                try:
                    scope, messages = _session_messages(
                        first, chain((first,), session_rows), cutoff, session_issues
                    )
                except HistoryDataError as exc:
                    issues.append(_issue(first, exc))
                    continue
                sources[scope].extend(messages)
                issues.extend(session_issues)
        # Release the history snapshot before checkpoint initialization or writes.
        batches = []
        skipped_sources = []
        checked = skipped = 0
        for scope in sorted(sources):
            messages = sources[scope]
            if not messages:
                continue
            versions = self._checkpoints.snapshot(
                scope, updater_version=self._updater_version
            )
            pending = [
                message
                for message in messages
                if versions.get(message.source_id) != message.source_version
            ]
            skipped_sources.extend(
                {
                    "source_id": message.source_id,
                    "status": "skipped",
                    "reason": "unchanged_source",
                }
                for message in messages
                if versions.get(message.source_id) == message.source_version
            )
            checked += len(messages)
            skipped += len(messages) - len(pending)
            if pending:
                batches.append(
                    UserChatBatch(
                        scope,
                        tuple(
                            sorted(
                                pending,
                                key=lambda item: (item.created_at, item.source_id),
                            )
                        ),
                    )
                )
        return ChatHistoryLoad(
            tuple(batches),
            tuple(sorted(issues, key=lambda item: (item["file"], item["reason"]))),
            checked,
            skipped,
            tuple(skipped_sources),
            {
                scope: {
                    message.message_id: message.created_at.isoformat(
                        timespec="microseconds"
                    )
                    for message in messages
                }
                for scope, messages in sources.items()
            },
        )

    def is_processed(
        self, scope: UserHistoryScope, message: ChatHistoryMessage
    ) -> bool:
        """Observe confirmations made since the initial history scan."""
        return self._checkpoints.is_processed(
            scope, message, updater_version=self._updater_version
        )

    def acknowledge(
        self,
        scope: UserHistoryScope,
        message: ChatHistoryMessage,
        *,
        processed_at: datetime,
    ) -> None:
        """Record the loaded revision only after this message's graph commit."""
        self._checkpoints.mark_processed(
            scope,
            message,
            updater_version=self._updater_version,
            processed_at=processed_at,
        )


def _history_rows(
    connection: HistoryConnection, cutoff: datetime, scope: UserHistoryScope | None
):
    before = connection.before
    parameters = {"cutoff": cutoff.isoformat() if connection.sqlite else cutoff}
    scope_filter = ""
    if scope is not None:
        parameters.update({field: getattr(scope, field) for field in _SCOPE_FIELDS})
        scope_filter = " AND " + " AND ".join(
            f"s.{field} = :{field}" for field in _SCOPE_FIELDS
        )
    eligible = " AND ".join(
        [
            "c.status IN ('completed', 'failed', 'canceled', 'cancelled')",
            *(
                before(f"c.{field}")
                for field in ("created_at", "updated_at", "completed_at")
            ),
        ]
    )
    rows = connection.rows(
        f"""
        SELECT s.id AS session_id, s.tenant_id AS session_tenant_id,
               s.workspace_id AS session_workspace_id, s.user_id AS session_user_id,
               s.created_at AS session_created_at, s.deleted_at AS session_deleted_at,
               c.id AS chat_id, c.tenant_id, c.workspace_id, c.user_id, c.sequence,
               c.status, c.created_at, c.updated_at, c.completed_at, c.datasource_id,
               CASE WHEN {eligible} THEN c.user_input END AS user_input,
               CASE WHEN {eligible} THEN 1 ELSE 0 END AS eligible
        FROM sessions s LEFT JOIN chats c ON c.session_id = s.id
        WHERE {before("s.created_at")} IS NOT FALSE
          AND ({before("s.created_at")} IS NULL OR s.deleted_at IS NULL
               OR {before("s.deleted_at")} IS NOT TRUE)
          {scope_filter}
        ORDER BY s.id, c.sequence, c.id
    """,
        parameters,
    )
    datasource_cache = {}
    for batch in iter(lambda: list(islice(rows, 256)), []):
        for row in batch:
            workspace = (row["session_tenant_id"], row["session_workspace_id"])
            if row["eligible"] and workspace not in datasource_cache:
                datasource_cache[workspace] = {
                    item["datasource_id"]: str(item["datasource_type"] or "")
                    .strip()
                    .lower()
                    for item in connection.rows(
                        "SELECT datasource_id, datasource_type FROM datasource "
                        "WHERE tenant_id = :tenant AND workspace_id = :workspace AND is_deleted = 0",
                        {"tenant": workspace[0], "workspace": workspace[1]},
                    )
                }
            row["datasource_types"] = datasource_cache.get(workspace, {})
        events = _batch_events(connection, batch)
        for row in batch:
            row["events"] = events.get(row["chat_id"], ())
            yield row


def _batch_events(connection: HistoryConnection, chats):
    ids = [
        row["chat_id"]
        for row in chats
        if row["eligible"] and "odps" in row["datasource_types"].values()
    ]
    if not ids:
        return {}
    parameters = {f"chat_{index}": value for index, value in enumerate(ids)}
    placeholders = ", ".join(f":{key}" for key in parameters)
    events = defaultdict(list)
    for row in connection.rows(
        f"""
        SELECT e.chat_id, e.sequence_number, e.payload, e.created_at
        FROM chat_events e JOIN chats c ON c.id = e.chat_id
          AND c.session_id = e.session_id AND c.tenant_id = e.tenant_id
          AND c.workspace_id = e.workspace_id AND c.user_id = e.user_id
        WHERE e.chat_id IN ({placeholders}) AND e.object = 'message'
        ORDER BY e.chat_id, e.sequence_number
        """,
        parameters,
    ):
        events[row["chat_id"]].append(row)
    return events


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoryDataError("invalid_source_scope", field)
    return value.strip()


def _session_messages(first, rows, cutoff, issues):
    session_id = _identity(first["session_id"], "session_id")
    created = parse_timestamp(first["session_created_at"], "sessions.created_at")
    deleted = first["session_deleted_at"]
    scope = UserHistoryScope(
        *(_identity(first[f"session_{field}"], field) for field in _SCOPE_FIELDS)
    )
    if created > cutoff or (
        deleted is not None
        and parse_timestamp(deleted, "sessions.deleted_at") <= cutoff
    ):
        return scope, []
    messages = []
    sequences = set()
    for row in rows:
        if row["chat_id"] is None:
            continue
        chat_id = _identity(row["chat_id"], "chat_id")
        created = parse_timestamp(row["created_at"], "chats.created_at")
        if created > cutoff:
            continue
        mismatched = [
            field
            for field in _SCOPE_FIELDS
            if _identity(row[field], field) != getattr(scope, field)
        ]
        if mismatched:
            raise HistoryDataError("chat_scope_mismatch", *mismatched)
        sequence = row["sequence"]
        if not isinstance(sequence, int) or sequence < 0 or sequence in sequences:
            raise HistoryDataError("invalid_chat_sequence", "sequence")
        sequences.add(sequence)
        if row["status"] not in {"completed", "failed", "canceled", "cancelled"}:
            continue
        if parse_timestamp(row["updated_at"], "chats.updated_at") > cutoff:
            continue
        if parse_timestamp(row["completed_at"], "chats.completed_at") > cutoff:
            continue
        raw_text = row["user_input"]
        if not isinstance(raw_text, str) or not raw_text.strip():
            issues.append(
                {
                    **_issue(
                        first, HistoryDataError("invalid_chat_user_input", "user_input")
                    ),
                    "status": "skipped",
                    "chat_id": chat_id,
                }
            )
            continue
        datasource_id = str(row["datasource_id"] or "").strip()
        datasource_type = row["datasource_types"].get(datasource_id, "")
        version = {"id": chat_id, "user_input": raw_text}
        if datasource_id:
            version.update(datasource_id=datasource_id, datasource_type=datasource_type)
        events = []
        for event in row["events"]:
            stamp = parse_timestamp(event["created_at"], "chat_events.created_at")
            if stamp <= cutoff:
                events.append({**event, "created_at": stamp})
        observations = odps_project_observations(
            events,
            chat_id=chat_id,
            chat_datasource_id=datasource_id,
            datasource_types=row["datasource_types"],
        )
        if observations:
            version["tool_observations"] = observations
        messages.append(
            ChatHistoryMessage(
                tool_observations=tuple(observations),
                message_id=f"host-chat:{chat_id}:user",
                text=raw_text.strip(),
                created_at=created,
                source_id="message:"
                + ":".join(
                    quote(value, safe="")
                    for value in (
                        *[getattr(scope, field) for field in _SCOPE_FIELDS],
                        session_id,
                        chat_id,
                    )
                ),
                source_version=sha256(
                    json.dumps(
                        version,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                context=tuple(
                    message.document()
                    for message in messages
                    if message.created_at <= created
                ),
                datasource_id=datasource_id,
                datasource_type=datasource_type,
            )
        )
    return scope, messages


def _issue(first, error: HistoryDataError) -> dict[str, str]:
    issue = {
        "file": first["session_id"] or "<invalid-session>",
        "status": "failed",
        "reason": error.reason,
    }
    if error.fields:
        issue["fields"] = ",".join(error.fields)
    if all(
        isinstance(first[f"session_{field}"], str) and first[f"session_{field}"].strip()
        for field in _SCOPE_FIELDS
    ):
        issue.update(
            {field: first[f"session_{field}"].strip() for field in _SCOPE_FIELDS}
        )
    return issue


__all__ = ["ChatLoader"]
