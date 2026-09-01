# -*- coding: utf-8 -*-
"""Data models for the BizTrace event stream and its Trace2Segment summaries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

BizEventKind = Literal[
    "user",
    "assistant_text",
    "assistant_thinking",
    "hint",
    "tool_use",
    "tool_result",
]
BizEventStatus = Literal["done", "error"]
BizChannel = Literal["main", "subagent"]
CardType = Literal["user", "thinking", "text", "hint", "tool"]

SegmentStatus = Literal["extracting", "done", "failed"]
BoundaryReason = Literal[
    "natural",
    "TaskStateUpdate",
    "PlanUpdate",
    "user_message",
    "ask_user_question",
    "max_span",
    "session_end",
]
ArtifactKind = Literal["query_script", "dataset", "report", "dashboard", "other"]
ArtifactRole = Literal["intermediate", "final", "supporting"]
# Host TaskStateUpdate states; pending never forces a segment boundary.
SubtaskState = Literal["pending", "in_progress", "completed"]
OrchestrationCategory = Literal["PlanCreate", "PlanUpdate", "TaskStateUpdate"]


class Presentation(BaseModel):
    """Backend-rendered card the frontend displays without protocol decisions."""

    card_type: CardType
    caption: str
    body: str = ""


class OrchestrationInfo(BaseModel):
    """Structured semantics of an orchestration tool call; None elsewhere."""

    category: OrchestrationCategory
    subtask_state: SubtaskState | None = None
    graph_id: str | None = None
    node_id: str | None = None
    node_name: str | None = None
    summary: str | None = None


class BizEvent(BaseModel):
    """One persisted business event, written once per RawTrace event."""

    event_id: str
    kind: BizEventKind
    channel: BizChannel = "main"
    parent_msg_id: str | None = None
    block_id: str | None = None
    seq: int = 0

    status: BizEventStatus = "done"

    tool_name: str | None = None
    input: Any = None
    output: Any = None
    content: str | None = None
    source: str | None = None
    orchestration: OrchestrationInfo | None = None
    presentation: Presentation | None = None

    started_at: float = 0.0
    ended_at: float | None = None


class FrontendEvent(BaseModel):
    """Frontend contract: the keys ``Envelope.send_biz_event`` accepts.

    The dump is splatted straight into ``BizEventRepository.upsert``, so an
    extra field is a TypeError rather than a silently ignored one. Chat and
    message level keys are absent by design: the Envelope owns that routing,
    and the frontend aligns on ``block_id`` alone.
    """

    event_id: str
    seq: int
    channel: BizChannel
    block_id: str | None
    status: BizEventStatus
    presentation: Presentation | None
    started_at: float
    ended_at: float | None

    @classmethod
    def of(cls, event: BizEvent) -> FrontendEvent:
        return cls(
            event_id=event.event_id,
            seq=event.seq,
            channel=event.channel,
            block_id=event.block_id,
            status=event.status,
            presentation=event.presentation,
            started_at=event.started_at,
            ended_at=event.ended_at,
        )


class BizTrace(BaseModel):
    """Read-side logical view of one session's business trace."""

    session_id: str
    events: list[BizEvent] = []

    def public_events(self) -> list[BizEvent]:
        """Externally visible view: sub-agent detail stays server-side."""
        return [event for event in self.events if event.channel != "subagent"]

    def frontend_events(self) -> list[FrontendEvent]:
        return [FrontendEvent.of(event) for event in self.public_events()]

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]], *, session_id: str) -> BizTrace:
        """Merge JSONL rows by event_id; the first row fixes seq and ordering."""
        merged: dict[str, BizEvent] = {}
        order: dict[str, int] = {}
        for row in rows:
            payload = row.get("event")
            if not isinstance(payload, dict):
                continue
            event = BizEvent.model_validate(payload)
            first = merged.get(event.event_id)
            if first is not None:
                event.seq = first.seq
            else:
                order[event.event_id] = event.seq
            merged[event.event_id] = event
        events = sorted(merged.values(), key=lambda e: order[e.event_id])
        return cls(session_id=session_id, events=events)


class Artifact(BaseModel):
    """A file the segment saved to the workspace; verified to exist on disk.

    ``kind`` and ``role`` are how the segmenter decides which files deserve a
    card — a query script always does, another file only as a final product —
    and stay on this side of the host boundary.
    """

    name: str
    description: str
    relative_path: str
    kind: ArtifactKind = "other"
    role: ArtifactRole = "supporting"


class SubtaskScope(BaseModel):
    """Plan task the segment belongs to, from TaskStateUpdate(in_progress)."""

    node_id: str
    node_name: str
    # Host plan tools have no plan-graph id; kept optional for older rows.
    graph_id: str | None = None


class Coverage(BaseModel):
    """BizTrace range a segment covers; the interval folds frontend cards."""

    start_seq: int
    end_seq: int
    event_ids: list[str] = []


class Segment(BaseModel):
    segment_id: str
    session_id: str
    status: SegmentStatus = "extracting"

    title: str | None = None
    input: str | None = None
    behavior: str | None = None
    conclusion: str | None = None
    artifact: list[Artifact] | None = None

    coverage: Coverage
    subtask: SubtaskScope | None = None
    boundary_reason: BoundaryReason
    forced_complete: bool = False

    started_at: float | None = None
    ended_at: float | None = None
    created_at: str = ""
    updated_at: str = ""


class FrontendCoverage(BaseModel):
    """Frontend folding key: the seq interval only."""

    start_seq: int
    end_seq: int


class FrontendArtifact(BaseModel):
    """Artifact contract the host stores: a name, a caption and a path.

    ``kind`` and ``role`` are left behind on purpose — they are how the
    segmenter picked these files, not something the host or the frontend acts
    on, and the host's segment schema rejects fields it does not know.
    """

    name: str
    description: str
    relative_path: str


class FrontendSegment(BaseModel):
    """Frontend contract, projected from done segments only."""

    segment_id: str
    title: str
    input: str | None
    behavior: str
    conclusion: str
    artifact: list[FrontendArtifact] | None
    coverage: FrontendCoverage
    started_at: float | None
    ended_at: float | None

    @classmethod
    def of(cls, segment: Segment) -> FrontendSegment | None:
        """Project a done segment; extracting / failed ones are not delivered."""
        if segment.status != "done":
            return None
        artifacts = [
            FrontendArtifact(
                name=item.name,
                description=item.description,
                relative_path=item.relative_path,
            )
            for item in segment.artifact or ()
        ]
        return cls(
            segment_id=segment.segment_id,
            title=segment.title or "",
            input=segment.input,
            behavior=segment.behavior or "",
            conclusion=segment.conclusion or "",
            artifact=artifacts or None,
            coverage=FrontendCoverage(
                start_seq=segment.coverage.start_seq,
                end_seq=segment.coverage.end_seq,
            ),
            started_at=segment.started_at,
            ended_at=segment.ended_at,
        )


__all__ = [
    "Artifact",
    "ArtifactKind",
    "ArtifactRole",
    "BizChannel",
    "BizEvent",
    "BizEventKind",
    "BizEventStatus",
    "BizTrace",
    "BoundaryReason",
    "CardType",
    "Coverage",
    "FrontendArtifact",
    "FrontendCoverage",
    "FrontendEvent",
    "FrontendSegment",
    "OrchestrationCategory",
    "OrchestrationInfo",
    "Presentation",
    "Segment",
    "SegmentStatus",
    "SubtaskScope",
    "SubtaskState",
]
