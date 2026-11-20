"""Define scoped chat messages and their independently tracked versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ...utils.validation import require_trimmed_text


@dataclass(frozen=True, slots=True, order=True)
class UserHistoryScope:
    """Identify one independently committed User Graph."""

    tenant_id: str
    workspace_id: str
    user_id: str

    def __post_init__(self) -> None:
        """Require a complete trusted source scope."""
        for field_name in ("tenant_id", "workspace_id", "user_id"):
            object.__setattr__(
                self,
                field_name,
                require_trimmed_text(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class ChatHistoryMessage:
    """Carry one extraction target and its earlier session context."""

    message_id: str  # Evidence reference exposed to the extractor.
    text: str  # User-authored target text.
    created_at: datetime  # Evidence time, independent of processing time.
    source_id: str  # Scoped session/chat checkpoint identity.
    source_version: str  # Hash of this target's extraction input.
    context: tuple[Mapping[str, str], ...] = ()  # Earlier same-session messages.
    datasource_id: str = ""  # Host-selected datasource, independent of prompt text.
    datasource_type: str = ""  # Active datasource type in this workspace.
    tool_observations: tuple[
        Mapping[str, object], ...
    ] = ()  # Validated execution facts.

    def __post_init__(self) -> None:
        """Validate finite algorithm input without provider metadata."""
        object.__setattr__(
            self,
            "message_id",
            require_trimmed_text(self.message_id, "message_id"),
        )
        object.__setattr__(
            self,
            "text",
            require_trimmed_text(self.text, "message text"),
        )
        for name in ("source_id", "source_version"):
            object.__setattr__(
                self, name, require_trimmed_text(getattr(self, name), name)
            )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "context", tuple(self.context))

    def document(self) -> dict[str, str]:
        """Expose user evidence and scoped datasource context to extraction."""
        return {
            "id": self.message_id,
            "text": self.text,
            "timestamp": self.created_at.isoformat(timespec="microseconds"),
            **({"datasource_id": self.datasource_id} if self.datasource_id else {}),
            **(
                {"datasource_type": self.datasource_type}
                if self.datasource_type
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class UserChatBatch:
    """Group independently committed targets for one authorized User."""

    scope: UserHistoryScope
    messages: tuple[ChatHistoryMessage, ...]

    def __post_init__(self) -> None:
        """Freeze a non-empty batch in evidence order."""
        if type(self.scope) is not UserHistoryScope:
            raise TypeError("scope must be a UserHistoryScope")
        messages = tuple(self.messages)
        if not messages or any(
            type(message) is not ChatHistoryMessage for message in messages
        ):
            raise TypeError("messages must contain ChatHistoryMessage values")
        ordered = tuple(
            sorted(
                messages,
                key=lambda message: (message.created_at, message.source_id),
            )
        )
        if messages != ordered:
            raise ValueError(
                "messages must be ordered by their evidence time and source id"
            )
        object.__setattr__(self, "messages", messages)


@dataclass(frozen=True, slots=True)
class ChatHistoryLoad:
    """Report pending messages, unchanged revisions and isolated source issues."""

    batches: tuple[UserChatBatch, ...]
    issues: tuple[Mapping[str, str], ...] = ()
    messages_checked: int = 0
    messages_skipped: int = 0
    skipped_sources: tuple[Mapping[str, str], ...] = ()
    evidence_times: Mapping[UserHistoryScope, Mapping[str, str]] = field(
        default_factory=dict
    )  # Clocks across all eligible sessions.


__all__ = [
    "ChatHistoryMessage",
    "ChatHistoryLoad",
    "UserChatBatch",
    "UserHistoryScope",
]
