# -*- coding: utf-8 -*-
"""RawTrace to BizTrace conversion.

The converter is a push state machine fed by ``enqueue``: raw AgentScope events
land in a bounded queue, a worker folds them into business events, and each
event is written once, as soon as it completes. A tool call and its result stay
two separate write-once cards, linked by their tool call id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import time
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import (
    BizEvent,
    BizEventKind,
    OrchestrationCategory,
    OrchestrationInfo,
    Presentation,
    SubtaskState,
)
from qwenpaw_data.host.core.algo.biztrace.presentation import CAPTIONS, PresentationBuilder
from qwenpaw_data.host.core.algo.biztrace.settings import CONVERTER_QUEUE_SIZE
from qwenpaw_data.host.core.algo.biztrace.tools import canonical_tool_name

logger = logging.getLogger(__name__)

ORCHESTRATION_CATEGORIES: dict[str, OrchestrationCategory] = {
    "PlanCreate": "PlanCreate",
    "PlanUpdate": "PlanUpdate",
    "TaskStateUpdate": "TaskStateUpdate",
}
# Exact TaskStateUpdate.state values from the host plan tools.
VALID_SUBTASK_STATES: frozenset[str] = frozenset(
    {"pending", "in_progress", "completed"}
)

# ToolResultEndEvent.state values that end a call without a usable result.
_FAILED_STATES: dict[str, str] = {
    "error": "failed_note",
    "interrupted": "interrupted_note",
    "denied": "denied_note",
}

# Agent-produced HintBlocks are wrapped so the LLM does not treat them as user text.
_SYSTEM_REMINDER_RE = re.compile(
    r"\A\s*<system-reminder>\s*(.*?)\s*</system-reminder>\s*\Z",
    re.DOTALL | re.IGNORECASE,
)

RowSink = Callable[[BizEvent], Awaitable[None]]
EventSink = Callable[[BizEvent], Awaitable[None]]


@dataclass(slots=True)
class _PendingRow:
    """A row waiting for its card before it can be written."""

    event: BizEvent
    card: asyncio.Task[Presentation]
    first_row: bool


@dataclass(slots=True)
class _BlockBuffer:
    """Accumulates one streaming text or thinking block."""

    kind: BizEventKind
    reply_id: str
    block_id: str
    parts: list[str] = field(default_factory=list)
    started_at: float = 0.0


@dataclass(slots=True)
class _PendingTool:
    """A tool call awaiting the result event that closes it."""

    tool_call_id: str
    tool_name: str
    reply_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    parts: list[str] = field(default_factory=list)
    tool_input: Any = None
    orchestration: OrchestrationInfo | None = None
    card: asyncio.Task[Presentation] | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    output_parts: list[str] = field(default_factory=list)
    output_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class _SubagentTool:
    event_id: str
    tool_name: str
    tool_input: Any
    card: asyncio.Task[Presentation] | None
    started_at: float


@dataclass(slots=True)
class _SubagentState:
    """Live nesting state for one ``spawn_subagent`` call."""

    parent_tool_call_id: str
    reply_id: str | None = None
    counter: int = 0
    thinking_id: str | None = None
    thinking_text: str = ""
    thinking_at: float = 0.0
    last_call_key: tuple[str, str] | None = None
    open_tools: list[_SubagentTool] = field(default_factory=list)


@dataclass(slots=True)
class ConversionStats:
    """Counters describing one conversion run."""

    rows: int = 0
    events: int = 0
    main_events: int = 0
    subagent_events: int = 0
    tool_calls: int = 0
    error_tools: int = 0
    orphan_results: int = 0
    unclosed_tools: int = 0
    subagent_chunks: int = 0
    orchestration_events: int = 0
    orchestration_unparsed: int = 0
    dropped_entries: int = 0


class BizTraceConverter:
    """Convert a raw AgentScope event flow into BizTrace rows.

    Args:
        presenter: Builds the presentation card of each emitted row.
        on_row: Writes one finished row; called in emission order.
        on_event: Post-emit hook. Receives only first-write main-channel
            events, matching the outward-facing view Trace2Segment consumes.
        lang: Language of the rule-based notes.
    """

    def __init__(
        self,
        *,
        presenter: PresentationBuilder,
        on_row: RowSink,
        on_event: EventSink | None = None,
        lang: str = "zh",
        queue_size: int = CONVERTER_QUEUE_SIZE,
    ) -> None:
        self.presenter = presenter
        self.on_row = on_row
        self.on_event = on_event
        self.words = CAPTIONS["en" if lang == "en" else "zh"]
        self.stats = ConversionStats()

        self._entries: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._rows: asyncio.Queue[_PendingRow | None] = asyncio.Queue()
        self._consumer: asyncio.Task[None] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

        self._seq = 0
        self._seq_by_event: dict[str, int] = {}
        self._blocks: dict[tuple[str, str], _BlockBuffer] = {}
        self._tools: dict[str, _PendingTool] = {}
        self._subagents: dict[str, _SubagentState] = {}
        self._plan_nodes: dict[str, str] = {}
        self._graph_id: str | None = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Spin up the conversion worker and the ordered row writer."""
        if self._consumer is None:
            self._consumer = asyncio.create_task(self._consume_loop())
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    def enqueue(self, entry: dict[str, Any]) -> None:
        """Take one raw entry. Synchronous, non-blocking, never raises."""
        if self._closed:
            return
        try:
            self._entries.put_nowait(entry)
        except asyncio.QueueFull:
            self.stats.dropped_entries += 1
            logger.warning(
                "BizTrace queue is full; dropped one raw entry (total %d)",
                self.stats.dropped_entries,
            )
        except Exception:
            logger.exception("Failed to enqueue a raw trace entry")

    async def aclose(self) -> None:
        """Drain the queue, reconcile unclosed tools, and stop the workers."""
        if self._closed:
            return
        self._closed = True
        self._entries.put_nowait(None)
        if self._consumer is not None:
            await self._consumer
            self._consumer = None
        self._reconcile_unclosed()
        self._rows.put_nowait(None)
        if self._writer is not None:
            await self._writer
            self._writer = None

    async def _consume_loop(self) -> None:
        while True:
            entry = await self._entries.get()
            if entry is None:
                return
            try:
                self._feed(entry)
            except Exception:
                logger.exception("Failed to convert a raw trace entry")

    async def _write_loop(self) -> None:
        while True:
            pending = await self._rows.get()
            if pending is None:
                return
            try:
                card = await pending.card
            except Exception:
                logger.exception("Presentation task failed; writing a bare row")
                card = None
            event = pending.event.model_copy(update={"presentation": card})
            try:
                await self.on_row(event)
            except Exception:
                logger.exception("BizEvent sink failed for %s", event.event_id)
            if not pending.first_row:
                # Reconciliation rewrites a row downstream already consumed.
                continue
            if self.on_event is not None and event.channel == "main":
                try:
                    await self.on_event(event)
                except Exception:
                    logger.exception(
                        "Post-emit hook failed for %s", event.event_id
                    )

    # -- ingestion --------------------------------------------------------- #

    def _feed(self, entry: dict[str, Any]) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        if entry.get("kind") == "user_input":
            self._on_user_message(payload)
            return

        event_type = str(payload.get("type") or "")
        at = _event_time(payload)
        if event_type in ("THINKING_BLOCK_START", "TEXT_BLOCK_START"):
            kind: BizEventKind = (
                "assistant_thinking"
                if event_type == "THINKING_BLOCK_START"
                else "assistant_text"
            )
            self._on_block_start(payload, kind, at)
        elif event_type in ("THINKING_BLOCK_DELTA", "TEXT_BLOCK_DELTA"):
            self._on_block_delta(payload)
        elif event_type in ("THINKING_BLOCK_END", "TEXT_BLOCK_END"):
            self._on_block_end(payload, at)
        elif event_type == "HINT_BLOCK":
            self._on_hint(payload, at)
        elif event_type == "TOOL_CALL_START":
            self._on_tool_call_start(payload, at)
        elif event_type == "TOOL_CALL_DELTA":
            self._on_tool_call_delta(payload)
        elif event_type == "TOOL_CALL_END":
            self._on_tool_call_end(payload, at)
        elif event_type == "TOOL_RESULT_START":
            self._on_tool_result_start(payload, at)
        elif event_type == "TOOL_RESULT_TEXT_DELTA":
            self._on_tool_result_text_delta(payload, at)
        elif event_type == "TOOL_RESULT_DATA_DELTA":
            self._on_tool_result_data_delta(payload)
        elif event_type == "TOOL_RESULT_END":
            self._on_tool_result_end(payload, at)

    # -- user, text and hint ----------------------------------------------- #

    def _on_user_message(self, payload: dict[str, Any]) -> None:
        message_id = str(payload.get("id") or "")
        blocks = [
            block
            for block in payload.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(str(block.get("text") or "") for block in blocks)
        block_id = next(
            (str(block["id"]) for block in blocks if block.get("id")), ""
        )
        event_id = block_id or f"{message_id}:0"
        at = _timestamp(payload.get("timestamp")) or time()
        self._emit(
            BizEvent(
                event_id=event_id,
                kind="user",
                parent_msg_id=message_id or None,
                block_id=event_id,
                content=text,
                started_at=at,
                ended_at=at,
            )
        )

    def _on_block_start(
        self, payload: dict[str, Any], kind: BizEventKind, at: float
    ) -> None:
        key = _block_key(payload)
        self._blocks[key] = _BlockBuffer(
            kind=kind, reply_id=key[0], block_id=key[1], started_at=at
        )

    def _on_block_delta(self, payload: dict[str, Any]) -> None:
        buffer = self._blocks.get(_block_key(payload))
        if buffer is not None:
            buffer.parts.append(str(payload.get("delta") or ""))

    def _on_block_end(self, payload: dict[str, Any], at: float) -> None:
        buffer = self._blocks.pop(_block_key(payload), None)
        if buffer is None:
            return
        self._emit(
            BizEvent(
                event_id=buffer.block_id,
                kind=buffer.kind,
                parent_msg_id=buffer.reply_id or None,
                block_id=buffer.block_id,
                content="".join(buffer.parts),
                started_at=buffer.started_at,
                ended_at=at,
            )
        )

    def _on_hint(self, payload: dict[str, Any], at: float) -> None:
        """A hint arrives complete in one event, so it is written immediately.

        Only Host steer (``source="steer"``) is user guidance. Every other
        HintBlock is an agent reminder and lands as ordinary assistant text.
        """
        block_id = str(payload.get("block_id") or "")
        hint = payload.get("hint")
        source = _as_str(payload.get("source"))
        content = _hint_text(hint)
        if source == "steer":
            event = BizEvent(
                event_id=block_id or f"hint:{payload.get('id')}",
                kind="hint",
                parent_msg_id=_as_str(payload.get("reply_id")),
                block_id=block_id or None,
                content=content,
                source=source,
                started_at=at,
                ended_at=at,
            )
            self._emit(event, self._card(event, hint=hint))
            return
        event = BizEvent(
            event_id=block_id or f"hint:{payload.get('id')}",
            kind="assistant_text",
            parent_msg_id=_as_str(payload.get("reply_id")),
            block_id=block_id or None,
            content=_unwrap_system_reminder(content),
            source=source,
            started_at=at,
            ended_at=at,
        )
        self._emit(event)

    # -- tool calls -------------------------------------------------------- #

    def _on_tool_call_start(self, payload: dict[str, Any], at: float) -> None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id:
            return
        # TOOL_CALL_END carries no name, so it is remembered from the start.
        self._tools[tool_call_id] = _PendingTool(
            tool_call_id=tool_call_id,
            tool_name=str(payload.get("tool_call_name") or "unknown_tool"),
            reply_id=_as_str(payload.get("reply_id")),
            metadata=payload.get("metadata") or {},
            started_at=at,
        )

    def _on_tool_call_delta(self, payload: dict[str, Any]) -> None:
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is not None:
            pending.parts.append(str(payload.get("delta") or ""))

    def _on_tool_call_end(self, payload: dict[str, Any], at: float) -> None:
        """Arguments are complete here, so the call event is final."""
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is None:
            return
        pending.tool_input = _parse_input("".join(pending.parts))
        pending.orchestration = self._parse_orchestration(pending)
        pending.ended_at = at
        call = self._tool_use_event(pending, status="done")
        pending.card = self._card(call)
        self.stats.tool_calls += 1
        self._emit(call, pending.card)

    def _on_tool_result_start(self, payload: dict[str, Any], at: float) -> None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        pending = self._tools.get(tool_call_id)
        if pending is None and tool_call_id:
            # A result without a call: keep the name so the card still reads.
            pending = _PendingTool(
                tool_call_id=tool_call_id,
                tool_name=str(payload.get("tool_call_name") or "unknown_tool"),
                reply_id=_as_str(payload.get("reply_id")),
                started_at=at,
            )
            self._tools[tool_call_id] = pending

    def _on_tool_result_text_delta(
        self, payload: dict[str, Any], at: float
    ) -> None:
        metadata = payload.get("metadata") or {}
        subagent_event = metadata.get("subagent_event")
        if subagent_event:
            self.stats.subagent_chunks += 1
            self._on_subagent_chunk(
                parent_tool_call_id=str(payload.get("tool_call_id") or ""),
                subagent_event=str(subagent_event),
                text=str(payload.get("delta") or ""),
                tool_name=metadata.get("subagent_tool_name"),
                at=at,
            )
            return
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is not None:
            pending.output_parts.append(str(payload.get("delta") or ""))

    def _on_tool_result_data_delta(self, payload: dict[str, Any]) -> None:
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is None:
            return
        block_id = str(payload.get("block_id") or "")
        media_type = str(payload.get("media_type") or "")
        block = pending.output_blocks.setdefault(
            block_id, {"type": "data", "source": {"media_type": media_type}}
        )
        url = payload.get("url")
        if isinstance(url, str) and url:
            block["source"]["url"] = url
            return
        chunk = payload.get("data")
        if isinstance(chunk, str) and chunk:
            source = block["source"]
            source["data"] = str(source.get("data") or "") + chunk

    def _on_tool_result_end(self, payload: dict[str, Any], at: float) -> None:
        """The result is its own event, linked to the call by their tool id."""
        tool_call_id = str(payload.get("tool_call_id") or "")
        pending = self._tools.pop(tool_call_id, None)
        subagent = self._subagents.pop(tool_call_id, None)
        if subagent is not None:
            self._close_subagent(subagent)
        if pending is None:
            self.stats.orphan_results += 1
            logger.warning("tool result without a matching call: %s", tool_call_id)

        raw_state = str(payload.get("state") or "success").lower()
        note_key = _FAILED_STATES.get(raw_state)
        if note_key is not None:
            self.stats.error_tools += 1
        result = BizEvent(
            event_id=f"{tool_call_id}:result",
            kind="tool_result",
            parent_msg_id=_as_str(payload.get("reply_id")),
            block_id=tool_call_id or None,
            status="error" if note_key else "done",
            tool_name=str(
                payload.get("tool_call_name")
                or (pending.tool_name if pending else "unknown_tool")
            ),
            output=_tool_output(pending),
            started_at=pending.started_at if pending else at,
            ended_at=at,
        )
        call_card = pending.card if pending is not None else None
        # "error" already reads as a failure on the card; the other two states
        # do not, so their reason is spelled out.
        note = (
            self.words[note_key]
            if note_key is not None and raw_state != "error"
            else None
        )
        self._emit(result, self._card(result, call_card=call_card, note=note))

    def _tool_use_event(self, pending: _PendingTool, *, status: str) -> BizEvent:
        return BizEvent(
            event_id=pending.tool_call_id,
            kind="tool_use",
            parent_msg_id=pending.reply_id,
            block_id=pending.tool_call_id,
            status=status,  # type: ignore[arg-type]
            tool_name=pending.tool_name,
            input=pending.tool_input,
            orchestration=pending.orchestration,
            started_at=pending.started_at,
            ended_at=pending.ended_at or pending.started_at,
        )

    def _reconcile_unclosed(self) -> None:
        """Rewrite calls whose result never arrived so none reads as fine."""
        for subagent in list(self._subagents.values()):
            self._close_subagent(subagent)
        self._subagents.clear()

        for pending in list(self._tools.values()):
            if pending.card is None:
                # The call event itself never completed; nothing was written.
                continue
            self.stats.unclosed_tools += 1
            event = self._tool_use_event(pending, status="error")
            self._emit(
                event,
                self._card(
                    event,
                    call_card=pending.card,
                    note=self.words["unclosed_note"],
                ),
            )
        self._tools.clear()

    # -- orchestration ----------------------------------------------------- #

    def _parse_orchestration(
        self, pending: _PendingTool
    ) -> OrchestrationInfo | None:
        category = ORCHESTRATION_CATEGORIES.get(
            canonical_tool_name(pending.tool_name)
        )
        if category is None:
            return None
        self.stats.orchestration_events += 1
        payload = pending.tool_input if isinstance(pending.tool_input, dict) else {}
        if category in ("PlanCreate", "PlanUpdate"):
            self._register_plan_nodes(payload)
        # Host plan tools do not carry a graph id; keep the field for older rows.
        graph_id = (
            _as_str(payload.get("graph_id"))
            or _as_str(pending.metadata.get("graph_id"))
            or self._graph_id
        )
        if graph_id:
            self._graph_id = graph_id
        node_id = (
            _as_str(payload.get("task_id"))
            or _as_str(payload.get("node_id"))
            or _as_str(pending.metadata.get("node_id"))
        )
        info = OrchestrationInfo(
            category=category,
            graph_id=graph_id,
            node_id=node_id,
            node_name=self._plan_nodes.get(node_id or ""),
            summary=_as_str(payload.get("summary"))
            or _as_str(payload.get("outcome")),
        )
        if category != "TaskStateUpdate":
            return info
        state = _as_str(payload.get("state"))
        if state not in VALID_SUBTASK_STATES:
            self.stats.orchestration_unparsed += 1
            logger.warning(
                "unparseable subtask state for %s: %r",
                pending.tool_name,
                payload.get("state"),
            )
            return None
        subtask_state: SubtaskState = state  # type: ignore[assignment]
        return info.model_copy(update={"subtask_state": subtask_state})

    def _register_plan_nodes(self, payload: dict[str, Any]) -> None:
        """Remember task_id → subject so later TaskStateUpdate cards can be named."""
        for task in payload.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            task_id = _as_str(task.get("id")) or _as_str(task.get("task_id"))
            name = _as_str(task.get("subject")) or _as_str(task.get("name"))
            if task_id and name:
                self._plan_nodes[task_id] = name
        for change in payload.get("changes") or []:
            if not isinstance(change, dict):
                continue
            task_id = _as_str(change.get("task_id")) or _as_str(change.get("node_id"))
            task = change.get("task") or change.get("node")
            if task_id and isinstance(task, dict):
                name = _as_str(task.get("subject")) or _as_str(task.get("name"))
                if name:
                    self._plan_nodes[task_id] = name
            elif task_id and change.get("action") == "delete":
                self._plan_nodes.pop(task_id, None)

    # -- sub-agent live nesting -------------------------------------------- #

    def _on_subagent_chunk(
        self,
        *,
        parent_tool_call_id: str,
        subagent_event: str,
        text: str,
        tool_name: Any,
        at: float,
    ) -> None:
        state = self._subagents.get(parent_tool_call_id)
        if state is None:
            parent = self._tools.get(parent_tool_call_id)
            state = _SubagentState(
                parent_tool_call_id=parent_tool_call_id,
                reply_id=parent.reply_id if parent is not None else None,
            )
            self._subagents[parent_tool_call_id] = state
        if subagent_event == "thinking":
            self._on_subagent_thinking(state, text, at)
        elif subagent_event == "tool_call":
            self._on_subagent_tool_call(
                state, str(tool_name or "unknown_tool"), text, at
            )
        elif subagent_event == "tool_result":
            self._on_subagent_tool_result(
                state, str(tool_name or "unknown_tool"), text, at
            )
        elif subagent_event == "summary":
            self._flush_subagent_thinking(state)

    def _on_subagent_thinking(
        self, state: _SubagentState, text: str, at: float
    ) -> None:
        """Accumulate one reasoning segment; chunks re-emit it as it grows."""
        if state.thinking_id is not None and text.startswith(state.thinking_text):
            state.thinking_text = text
            return
        self._flush_subagent_thinking(state)
        state.thinking_id = self._next_subagent_id(state)
        state.thinking_text = text
        state.thinking_at = at

    def _flush_subagent_thinking(self, state: _SubagentState) -> None:
        if state.thinking_id is None:
            return
        self._emit(
            BizEvent(
                event_id=state.thinking_id,
                kind="assistant_thinking",
                channel="subagent",
                parent_msg_id=state.reply_id,
                block_id=state.parent_tool_call_id,
                content=state.thinking_text,
                started_at=state.thinking_at,
                ended_at=state.thinking_at,
            )
        )
        state.thinking_id = None
        state.thinking_text = ""

    def _on_subagent_tool_call(
        self, state: _SubagentState, tool_name: str, text: str, at: float
    ) -> None:
        payload = _parse_subagent_call_input(text)
        key = (tool_name, payload)
        if state.last_call_key == key:
            return
        state.last_call_key = key
        # Acting ends the reasoning segment, so ids follow emission order.
        self._flush_subagent_thinking(state)
        event_id = self._next_subagent_id(state)
        event = BizEvent(
            event_id=event_id,
            kind="tool_use",
            channel="subagent",
            parent_msg_id=state.reply_id,
            block_id=event_id,
            tool_name=tool_name,
            input=_parse_input(payload),
            started_at=at,
            ended_at=at,
        )
        card = self._card(event)
        state.open_tools.append(
            _SubagentTool(
                event_id=event.event_id,
                tool_name=tool_name,
                tool_input=event.input,
                card=card,
                started_at=at,
            )
        )
        self._emit(event, card)

    def _on_subagent_tool_result(
        self, state: _SubagentState, tool_name: str, text: str, at: float
    ) -> None:
        self._flush_subagent_thinking(state)
        state.last_call_key = None
        pending = next(
            (
                tool
                for tool in reversed(state.open_tools)
                if tool.tool_name == tool_name
            ),
            None,
        )
        if pending is None:
            self.stats.orphan_results += 1
            return
        state.open_tools.remove(pending)
        # Sub-agent chunks carry no error flag, so a closed call reads as done.
        event = BizEvent(
            event_id=f"{pending.event_id}:result",
            kind="tool_result",
            channel="subagent",
            parent_msg_id=state.reply_id,
            block_id=pending.event_id,
            tool_name=pending.tool_name,
            output=text,
            started_at=at,
            ended_at=at,
        )
        call_card = pending.card
        self._emit(event, self._card(event, call_card=call_card))

    def _close_subagent(self, state: _SubagentState) -> None:
        self._flush_subagent_thinking(state)
        for pending in state.open_tools:
            event = BizEvent(
                event_id=pending.event_id,
                kind="tool_use",
                channel="subagent",
                parent_msg_id=state.reply_id,
                block_id=pending.event_id,
                status="error",
                tool_name=pending.tool_name,
                input=pending.tool_input,
                started_at=pending.started_at,
                ended_at=pending.started_at,
            )
            self._emit(
                event,
                self._card(
                    event,
                    call_card=pending.card,
                    note=self.words["unclosed_note"],
                ),
            )
        state.open_tools.clear()

    def _next_subagent_id(self, state: _SubagentState) -> str:
        state.counter += 1
        return f"{state.parent_tool_call_id}:{state.counter}"

    # -- emission ---------------------------------------------------------- #

    def _card(
        self,
        event: BizEvent,
        *,
        call_card: asyncio.Task[Presentation] | None = None,
        note: str | None = None,
        hint: Any = None,
    ) -> asyncio.Task[Presentation]:
        """Schedule the card so independent cards are produced in parallel."""

        async def build() -> Presentation:
            base = await call_card if call_card is not None else None
            return await self.presenter.build(
                event, call_card=base, note=note, hint=hint
            )

        return asyncio.create_task(build())

    def _emit(
        self, event: BizEvent, card: asyncio.Task[Presentation] | None = None
    ) -> None:
        previous_seq = self._seq_by_event.get(event.event_id)
        first_row = previous_seq is None
        if first_row:
            self._seq += 1
            self._seq_by_event[event.event_id] = self._seq
            seq = self._seq
        else:
            # A rewrite keeps its original slot so cards never move.
            seq = previous_seq  # type: ignore[assignment]
        event = event.model_copy(update={"seq": seq})

        self.stats.rows += 1
        if first_row:
            self.stats.events += 1
            if event.channel == "subagent":
                self.stats.subagent_events += 1
            else:
                self.stats.main_events += 1

        self._rows.put_nowait(
            _PendingRow(
                event=event,
                card=card if card is not None else self._card(event),
                first_row=first_row,
            )
        )


def _block_key(payload: dict[str, Any]) -> tuple[str, str]:
    return str(payload.get("reply_id") or ""), str(payload.get("block_id") or "")


def _hint_text(hint: Any) -> str:
    """Text view of a hint; DataBlocks stay out and land on the card only."""
    if isinstance(hint, str):
        return hint
    if not isinstance(hint, list):
        return ""
    return "\n\n".join(
        str(block.get("text") or "")
        for block in hint
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _unwrap_system_reminder(text: str) -> str:
    """Drop ``<system-reminder>`` tags when an agent hint was wrapped for the LLM."""
    match = _SYSTEM_REMINDER_RE.match(text)
    return match.group(1).strip() if match else text


def _tool_output(pending: _PendingTool | None) -> Any:
    """Assemble the accumulated result payload of one tool call."""
    if pending is None:
        return None
    text = "".join(pending.output_parts)
    if not pending.output_blocks:
        return text
    blocks: list[dict[str, Any]] = list(pending.output_blocks.values())
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def _parse_input(text: str) -> Any:
    """Decode aggregated tool arguments, keeping the raw text when invalid."""
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _parse_subagent_call_input(text: str) -> str:
    """Extract the payload from a ``[tool_call] name(payload)`` chunk."""
    start = text.find("(")
    if start == -1 or not text.endswith(")"):
        return text
    return text[start + 1 : -1]


def _event_time(payload: dict[str, Any]) -> float:
    return _timestamp(payload.get("created_at")) or time()


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["BizTraceConverter", "ConversionStats"]
