# -*- coding: utf-8 -*-
"""Online segmentation of a BizTrace.

Main-channel BizEvents are folded into chain nodes — one per assistant message
by default, split apart at hard boundaries — a sliding window accumulates them,
a first LLM step decides continuity, and a second step extracts the metadata of
each delimited window.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from qwenpaw_data.host.core.algo.biztrace.formatting import compact, output_text, truncate
from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM, StructuredLLMError
from qwenpaw_data.host.core.algo.biztrace.models import (
    BizEvent,
    BoundaryReason,
    Coverage,
    Segment,
    SubtaskScope,
)
from qwenpaw_data.host.core.algo.biztrace.presentation import Linker
from qwenpaw_data.host.core.algo.biztrace.prompts import (
    DEFAULT_PROMPT_LANG,
    PromptLang,
    build_continuity_user_prompt,
    build_extractor_user_prompt,
    get_continuity_system_prompt,
    get_extractor_system_prompt,
)
from qwenpaw_data.host.core.algo.biztrace.settings import FLUSH_BUDGET_SECONDS
from qwenpaw_data.host.core.algo.biztrace.tools import (
    ASK_USER_QUESTION,
    canonical_tool_name,
)
from qwenpaw_data.host.core.algo.biztrace.workspace_index import (
    ArtifactProposal,
    ArtifactVerifier,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SPAN = 100
TITLE_CHAR_CAP = 200
# Tail of the tool id shown in a window, enough to pair a call with its result.
CALL_KEY_CHARS = 6

TEXT_CHAR_CAP = 1_500
THINKING_CHAR_CAP = 400
HINT_CHAR_CAP = 800
TOOL_INPUT_CHAR_CAP = 1_200
TOOL_OUTPUT_CHAR_CAP = 2_000
WINDOW_CHAR_CAP = 24_000
WINDOW_TRUNCATION_MARK = "\n\n...[window truncated for length]...\n\n"

_LIST_MARKER_RE = re.compile(r"^(?:[-*]|\d+\.)\s+")

NodeKind = Literal["normal", "user", "hint", "ask", "orchestration"]
NodeRole = Literal["user", "assistant", "hint"]

# Host TaskStateUpdate states that force a boundary; "pending" is bookkeeping.
BOUNDARY_SUBTASK_STATES = frozenset({"in_progress", "completed"})

_CONTINUITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "continues": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["continues", "reason"],
}

_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "input": {"type": ["string", "null"]},
        "behavior": {"type": "string"},
        "conclusion": {"type": "string"},
        "artifact": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "query_script",
                            "dataset",
                            "report",
                            "dashboard",
                            "other",
                        ],
                    },
                    "role": {
                        "type": "string",
                        "enum": ["intermediate", "final", "supporting"],
                    },
                },
                "required": ["name", "description", "kind", "role"],
            },
        },
    },
    # The nullable two are asked for in the prompt but not required here: a small
    # model that drops a key it would have set to null is answering correctly,
    # and the transport validates the schema strictly enough to fail the segment
    # over it.
    "required": ["title", "behavior", "conclusion"],
}

SegmentSink = Callable[[Segment], Awaitable[None]]
JudgeLogSink = Callable[[dict[str, Any]], Awaitable[None]]

# Files produced inside a coverage seq range (from host artifact_delta).
# Declared here rather than imported from the host runtime to avoid a cycle.
ArtifactFiles = Callable[[int, int], dict[str, str]]


def no_artifact_files(start_seq: int = 0, end_seq: int = 0) -> dict[str, str]:
    """Stand in when a pipeline has no host file feed."""
    _ = (start_seq, end_seq)
    return {}


# --------------------------------------------------------------------------- #
# Chain building
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SegmentChainNode:
    """One judgment unit: a whole message bubble, or a fragment of one."""

    index: int
    kind: NodeKind
    role: NodeRole
    parent_msg_id: str | None
    covered: bool
    content: list[dict[str, Any]] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0
    orchestration: dict[str, Any] | None = None


class ChainBuilder:
    """Fold BizEvents into chain nodes: aggregate by message, split at boundaries.

    Events stream in, so a node is only complete once an event from another
    message arrives or a hard boundary interrupts it; ``push`` therefore
    returns the nodes it closed, and ``flush`` releases the last one.
    """

    def __init__(self) -> None:
        self._open: SegmentChainNode | None = None
        self._pending_ask: SegmentChainNode | None = None
        # Boundary TaskStateUpdate nodes stay mutable so their tool_result can
        # join the same covered node (FE hides by seq range).
        self._pending_task_state: SegmentChainNode | None = None
        self._index = 0

    def push(self, event: BizEvent) -> list[SegmentChainNode]:
        """Add one main-channel BizEvent, returning the nodes it closed."""
        tool = canonical_tool_name(event.tool_name)

        if event.kind == "tool_use" and tool == ASK_USER_QUESTION:
            closed = self._close_open()
            # Uncovered until the user answers, so an unanswered question cannot
            # emit a segment. The node is released now but stays open for the
            # result that marks it covered.
            node = self._new_node(kind="ask", role="assistant", covered=False)
            self._add(node, event)
            self._pending_ask = node
            self._pending_task_state = None
            return [*closed, node]

        if (
            event.kind == "tool_result"
            and tool == ASK_USER_QUESTION
            and self._pending_ask is not None
        ):
            self._add(self._pending_ask, event)
            self._pending_ask.covered = True
            return []

        if (
            event.kind == "tool_result"
            and tool == "TaskStateUpdate"
            and self._pending_task_state is not None
        ):
            # Release only once the result is attached so coverage seqs include
            # both ends of the tool pair (FE folds by seq range).
            self._add(self._pending_task_state, event)
            node = self._pending_task_state
            self._pending_task_state = None
            return [node]

        split = _split_kind(event)
        if split is not None:
            self._pending_ask = None
            closed = self._close_open()
            # TaskStateUpdate is covered so FE can fold it under a segment, but
            # the assembler keeps it out of judge/extractor prompts. Hold the
            # node until its tool_result (or flush) so both seqs are covered.
            node = self._new_node(
                kind=split,
                role="hint" if split == "hint" else "assistant",
                covered=split != "hint",
            )
            self._add(node, event)
            if split == "orchestration" and event.orchestration is not None:
                node.orchestration = event.orchestration.model_dump(mode="json")
                if event.orchestration.category == "TaskStateUpdate":
                    self._pending_task_state = node
                    return closed
            self._pending_task_state = None
            return [*closed, node]

        if event.kind == "user":
            self._pending_ask = None
            self._pending_task_state = None
            closed = self._close_open()
            node = self._new_node(kind="user", role="user", covered=False)
            self._add(node, event)
            return [*closed, node]

        if _event_block(event) is None:
            return []
        self._pending_ask = None
        self._pending_task_state = None
        switched: list[SegmentChainNode] = []
        if self._open is not None and event.parent_msg_id != self._open.parent_msg_id:
            switched = self._close_open()
        if self._open is None:
            self._open = self._new_node(
                kind="normal", role="assistant", covered=True
            )
        self._add(self._open, event)
        return switched

    def flush(self) -> list[SegmentChainNode]:
        """Release whatever is still accumulating."""
        self._pending_ask = None
        pending = self._pending_task_state
        self._pending_task_state = None
        closed = self._close_open()
        return [*closed, pending] if pending is not None else closed

    def _close_open(self) -> list[SegmentChainNode]:
        node = self._open
        self._open = None
        return [node] if node is not None else []

    def _new_node(
        self, *, kind: NodeKind, role: NodeRole, covered: bool
    ) -> SegmentChainNode:
        node = SegmentChainNode(
            index=self._index,
            kind=kind,
            role=role,
            parent_msg_id=None,
            covered=covered,
        )
        self._index += 1
        return node

    def _add(self, node: SegmentChainNode, event: BizEvent) -> None:
        block = _event_block(event)
        if block is not None:
            node.content.append(block)
        if node.parent_msg_id is None:
            node.parent_msg_id = event.parent_msg_id
        node.event_ids.append(event.event_id)
        node.seqs.append(event.seq)
        if not node.started_at:
            node.started_at = event.started_at
        node.ended_at = event.ended_at or event.started_at


def _split_kind(event: BizEvent) -> Literal["hint", "orchestration"] | None:
    """Return the hard-boundary kind this event triggers, if any."""
    if event.kind == "hint":
        return "hint"
    if event.kind != "tool_use" or event.orchestration is None:
        return None
    category = event.orchestration.category
    if category == "PlanUpdate":
        return "orchestration"
    if category == "TaskStateUpdate":
        state = event.orchestration.subtask_state
        return "orchestration" if state in BOUNDARY_SUBTASK_STATES else None
    return None


def _event_block(event: BizEvent) -> dict[str, Any] | None:
    """Map one BizEvent to a chain content block, or None to drop it."""
    if event.kind in ("tool_use", "tool_result"):
        name = canonical_tool_name(event.tool_name) or "unknown_tool"
        # A call and its result are separate events, so the tool id pairs them
        # back up in the rendered window.
        call = (event.block_id or "")[-CALL_KEY_CHARS:]
        if event.kind == "tool_result":
            return {
                "type": "tool_result",
                "name": name,
                "call": call,
                "output": event.output,
                "status": event.status,
            }
        block: dict[str, Any] = {
            "type": "tool_use",
            "name": name,
            "call": call,
            "input": event.input,
        }
        if event.orchestration is not None:
            block["orchestration"] = event.orchestration.model_dump(mode="json")
        return block
    text = (event.content or "").strip()
    if event.kind == "hint":
        # A hint clears the window even when it carries no readable text.
        return {"type": "hint", "text": text}
    if not text:
        return None
    if event.kind == "assistant_thinking":
        return {"type": "thinking", "text": text}
    return {"type": "text", "text": text}


# --------------------------------------------------------------------------- #
# Window rendering
# --------------------------------------------------------------------------- #


def render_window(nodes: list[SegmentChainNode]) -> str:
    """Render a contiguous window as the two LLM steps read it."""
    parts = [
        "\n".join(
            [
                f"[{node.index}] {node.role}",
                *(line for line in map(render_block, node.content) if line),
            ]
        )
        for node in nodes
    ]
    return _cap_window("\n\n".join(parts))


def render_block(block: dict[str, Any]) -> str:
    """Render one content block as a single capped line."""
    block_type = block.get("type")
    if block_type == "text":
        return f"text: {truncate(str(block.get('text') or ''), TEXT_CHAR_CAP)}"
    if block_type == "thinking":
        return (
            f"thinking: {truncate(str(block.get('text') or ''), THINKING_CHAR_CAP)}"
        )
    if block_type == "hint":
        return f"hint: {truncate(str(block.get('text') or ''), HINT_CHAR_CAP)}"
    label = _tool_label(block)
    if block_type == "tool_use":
        payload = truncate(compact(block.get("input")), TOOL_INPUT_CHAR_CAP)
        return f"tool_use {label}: {payload}"
    if block_type == "tool_result":
        payload = truncate(output_text(block.get("output")), TOOL_OUTPUT_CHAR_CAP)
        suffix = " [error]" if block.get("status") == "error" else ""
        return f"tool_result {label}: {payload}{suffix}"
    return f"{block_type or 'unknown'}: {truncate(compact(block), TEXT_CHAR_CAP)}"


def _tool_label(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "unknown_tool")
    call = block.get("call")
    return f"{name}#{call}" if call else name


def _cap_window(rendered: str) -> str:
    """Keep the head and tail of an over-budget rendering, marking the cut."""
    if len(rendered) <= WINDOW_CHAR_CAP:
        return rendered
    keep = max(WINDOW_CHAR_CAP // 2 - 40, 200)
    return rendered[:keep] + WINDOW_TRUNCATION_MARK + rendered[-keep:]


# --------------------------------------------------------------------------- #
# Step 1: ContinuityJudge
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ContinuityDecision:
    continues: bool
    reason: str


class ContinuityJudge:
    """Decide whether a peek node continues the current window."""

    def __init__(
        self,
        *,
        llm: StructuredLLM | None = None,
        lang: PromptLang = DEFAULT_PROMPT_LANG,
    ) -> None:
        self.llm = llm
        self.lang = lang
        self.query = ""
        self.degraded = 0

    async def judge(
        self,
        window: list[SegmentChainNode],
        peek: SegmentChainNode,
        *,
        scope: SubtaskScope | None,
    ) -> ContinuityDecision:
        """Return the decision, degrading to "continues" on any failure.

        A failed call keeps the window accumulating so ``max_span`` remains
        the backstop; cutting on failure would shred the trace instead.
        """

        if self.llm is not None:
            user = build_continuity_user_prompt(
                query=self.query,
                scope=_scope_text(scope),
                start_index=window[0].index,
                end_index=window[-1].index,
                rendered_window=render_window(window),
                peek_index=peek.index,
                rendered_peek=render_window([peek]),
                lang=self.lang,
            )
            try:
                data = await self.llm.complete(
                    system=get_continuity_system_prompt(self.lang),
                    user=user,
                    schema_name="continuity_decision",
                    schema=_CONTINUITY_SCHEMA,
                )
                reason = str(data.get("reason") or "").strip()
                return ContinuityDecision(
                    continues=bool(data.get("continues")), reason=reason
                )
            except (StructuredLLMError, ValueError) as exc:
                logger.warning("continuity judgment degraded to continue: %s", exc)
        self.degraded += 1
        return ContinuityDecision(
            continues=True, reason="judge unavailable; degraded"
        )


# --------------------------------------------------------------------------- #
# Step 2: SegmentExtractor
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ExtractionOutcome:
    """Outcome of one metadata extraction attempt."""

    ok: bool
    fields: dict[str, Any] = field(default_factory=dict)

    def log_fields(self) -> dict[str, Any]:
        payload = dict(self.fields)
        artifacts = payload.get("artifact")
        if artifacts:
            payload["artifact"] = [item.model_dump(mode="json") for item in artifacts]
        return payload


class SegmentExtractor:
    """Extract title / input / behavior / conclusion / artifact for a window."""

    def __init__(
        self,
        *,
        llm: StructuredLLM | None = None,
        lang: PromptLang = DEFAULT_PROMPT_LANG,
        verifier: ArtifactVerifier | None = None,
        linker: Linker | None = None,
    ) -> None:
        self.llm = llm
        self.lang = lang
        self.verifier = verifier if verifier is not None else ArtifactVerifier()
        self.linker = linker
        self.query = ""
        self.failures = 0

    async def extract(
        self,
        window: list[SegmentChainNode],
        *,
        scope: SubtaskScope | None,
        boundary_reason: BoundaryReason,
        files: dict[str, str],
    ) -> ExtractionOutcome:
        """Summarize a frozen window snapshot into segment metadata."""
        if self.llm is None:
            self.failures += 1
            return ExtractionOutcome(ok=False)
        user = build_extractor_user_prompt(
            query=self.query,
            scope=_scope_text(scope),
            boundary_reason=boundary_reason,
            rendered_window=render_window(window),
            candidate_files=list(files),
            lang=self.lang,
        )
        try:
            data = await self.llm.complete(
                system=get_extractor_system_prompt(self.lang),
                user=user,
                schema_name="segment_metadata",
                schema=_EXTRACTION_SCHEMA,
            )
        except StructuredLLMError as exc:
            self.failures += 1
            logger.warning("segment extraction failed: %s", exc)
            return ExtractionOutcome(ok=False)

        behavior = _text_field(data.get("behavior"))
        conclusion = _text_field(data.get("conclusion"))
        if not behavior or not conclusion:
            self.failures += 1
            logger.warning("segment extraction missing required fields")
            return ExtractionOutcome(ok=False)
        artifacts = self.verifier.verify(
            _artifact_proposals(data.get("artifact")), files=files
        )
        # Strip every in-window candidate filename from input — not only the
        # verified artifact names. Models pad input with outputs even when they
        # omit those files from the artifact array.
        produced = set(files) | {
            item.name for item in (artifacts or []) if item.name
        }
        return ExtractionOutcome(
            ok=True,
            fields={
                # Titles and artifact names stay literal; only the three prose
                # fields carry entity links.
                "title": _text_field(data.get("title")) or _title_from(behavior),
                "input": self._link(
                    _strip_artifact_names_from_input(
                        _text_field(data.get("input")), produced
                    )
                ),
                "behavior": self._link(behavior),
                "conclusion": self._link(conclusion),
                "artifact": artifacts,
            },
        )

    def _link(self, text: str | None) -> str | None:
        """Inject entity links, falling back to the raw text on any failure."""
        if self.linker is None or not text:
            return text
        try:
            return self.linker(text)
        except Exception:
            logger.exception("Entity linking failed; keeping the raw field")
            return text


def _artifact_proposals(raw: Any) -> Iterator[ArtifactProposal]:
    """Yield the model's artifact entries; the verifier decides which are real."""
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        yield ArtifactProposal(
            name=name,
            description=_clean(item.get("description")) or "",
            kind=_clean(item.get("kind")) or "",
            role=_clean(item.get("role")) or "",
        )


# --------------------------------------------------------------------------- #
# Sliding window state machine
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SegmentStats:
    """Counters describing one segmentation run."""

    rows: int = 0
    segments: int = 0
    chain_nodes: int = 0
    judge_calls: int = 0
    degraded: int = 0
    failed: int = 0
    cleared_by_hint: int = 0
    boundaries: dict[str, int] = field(default_factory=dict)


class SegmentAssembler:
    """Consume BizEvents and write two-phase segment rows.

    Args:
        session_id: Session the segments belong to.
        judge: Step-one continuity judge.
        extractor: Step-two metadata extractor.
        on_row: Writes every segment row, both phases.
        on_segment: Delivers a finished segment to the host; done rows only.
        on_judge_log: Receives continuity and extraction log lines.
        max_span: Window size, in chain nodes, that forces a cut.
        artifact_files: ``(start_seq, end_seq) ->`` files produced in that range.
    """

    def __init__(
        self,
        *,
        session_id: str,
        judge: ContinuityJudge,
        extractor: SegmentExtractor,
        on_row: SegmentSink,
        on_segment: SegmentSink | None = None,
        on_judge_log: JudgeLogSink | None = None,
        max_span: int = DEFAULT_MAX_SPAN,
        concurrency: int = 2,
        artifact_files: ArtifactFiles | None = None,
    ) -> None:
        self.session_id = session_id
        self.judge = judge
        self.extractor = extractor
        self.on_row = on_row
        self.on_segment = on_segment
        self.on_judge_log = on_judge_log
        self.max_span = max(1, max_span)
        self.artifact_files = (
            artifact_files if artifact_files is not None else no_artifact_files
        )
        self.stats = SegmentStats()

        self._chain = ChainBuilder()
        self._window: list[SegmentChainNode] = []
        self._scope: SubtaskScope | None = None
        self._seq = 0
        self._segment_no = 0
        self._seen_node = False
        self._extractions: dict[asyncio.Task[None], Segment] = {}
        self._slots = asyncio.Semaphore(max(1, concurrency))

    async def feed(self, event: BizEvent) -> None:
        """Consume one main-channel BizEvent."""
        for node in self._chain.push(event):
            await self._on_node(node)

    async def aclose(self, *, budget: float = FLUSH_BUDGET_SECONDS) -> None:
        """Flush the chain, delimit the remainder and wait out the extractions."""
        for node in self._chain.flush():
            await self._on_node(node)
        if self._window:
            await self._delimit("session_end", forced=True)
        await self._wait_extractions(budget)

    # -- core loop --------------------------------------------------------- #

    async def _on_node(self, node: SegmentChainNode) -> None:
        self.stats.chain_nodes += 1
        if not self._seen_node:
            self._seen_node = True
            if node.kind == "user":
                # The opening user message is the global task itself, injected
                # into both prompts as the query rather than judged.
                self._set_query(node)
                return

        if node.kind == "hint":
            # Guidance invalidates the draft it interrupts: the window is
            # cleared without a segment, and the hint frames what follows.
            if _covered_nodes(self._window):
                self.stats.cleared_by_hint += 1
            self._window = [node]
            return

        if node.kind == "user":
            self._set_query(node)
            await self._delimit("user_message", forced=True)
            self._open_window(node)
            return

        if node.kind == "ask":
            await self._delimit("ask_user_question", forced=True)
            self._open_window(node)
            return

        if node.kind == "orchestration":
            await self._on_plan_boundary(node)
            return

        summary_window = _summary_nodes(self._window)
        if not summary_window:
            # Nothing judgeable yet: empty, framing-only, or only TaskStateUpdate
            # bookkeeping waiting to be absorbed into the next productive stretch.
            self._open_window(node)
            return

        decision = await self.judge.judge(summary_window, node, scope=self._scope)
        self.stats.judge_calls += 1
        await self._log_continuity(node, decision)

        if decision.continues:
            self._window.append(node)
            if len(self._window) >= self.max_span:
                await self._delimit("max_span", forced=True)
            return
        await self._delimit("natural", forced=False)
        self._open_window(node)

    def _set_query(self, node: SegmentChainNode) -> None:
        query = "\n".join(
            str(block.get("text") or "")
            for block in node.content
            if block.get("type") == "text"
        ).strip()
        if query:
            self.judge.query = query
            self.extractor.query = query

    def _open_window(self, node: SegmentChainNode) -> None:
        # Context a non-productive delimit left behind leads the new window.
        self._window = [*self._window, node]

    async def _on_plan_boundary(self, node: SegmentChainNode) -> None:
        """Plan events cut deterministically, without a continuity call.

        ``TaskStateUpdate`` force-cuts and updates scope. The node is covered so
        the frontend can fold it under a segment, but summary/judge prompts skip
        it. ``PlanUpdate`` still opens the next stretch as its frame.
        """
        info = node.orchestration or {}
        if info.get("category") == "PlanUpdate":
            # The revision announces the next stretch of work, so it opens it.
            await self._delimit("PlanUpdate", forced=True)
            self._open_window(node)
            return
        if info.get("subtask_state") == "in_progress":
            await self._delimit("TaskStateUpdate", forced=True)
            self._scope = _build_scope(info)
            # Lead the next stretch so its coverage absorbs this bookkeeping.
            self._open_window(node)
            return
        self._window.append(node)
        if not _productive_nodes(self._window):
            # No productive work yet; keep the node so a later segment (or
            # session_end with real work) can absorb it into coverage.
            self._scope = None
            return
        await self._delimit("TaskStateUpdate", forced=True)
        self._scope = None

    async def _delimit(self, reason: BoundaryReason, *, forced: bool) -> None:
        """Freeze the window, write the placeholder row, then extract metadata."""
        snapshot = self._window
        covered = _covered_nodes(snapshot)
        productive = _productive_nodes(snapshot)
        if not productive:
            # Only framing / TaskStateUpdate bookkeeping: keep it in the window
            # so a later productive segment can absorb those seqs for the FE.
            return
        self._window = []

        self._segment_no += 1
        now = _now()
        # Coverage includes TaskStateUpdate so the frontend hides those cards;
        # extraction below sees only the productive summary nodes.
        event_ids = [eid for node in covered for eid in node.event_ids]
        seqs = [seq for node in covered for seq in node.seqs]
        summary = _summary_nodes(snapshot)
        scope = self._scope
        segment = Segment(
            segment_id=f"seg_{self._segment_no:04d}",
            session_id=self.session_id,
            status="extracting",
            coverage=Coverage(
                start_seq=min(seqs), end_seq=max(seqs), event_ids=event_ids
            ),
            subtask=scope,
            boundary_reason=reason,
            forced_complete=forced,
            started_at=covered[0].started_at,
            ended_at=covered[-1].ended_at or covered[-1].started_at,
            created_at=now,
            updated_at=now,
        )
        self.stats.segments += 1
        self.stats.boundaries[reason] = self.stats.boundaries.get(reason, 0) + 1
        # The placeholder persists the boundary immediately but is not
        # delivered: the host only publishes segments whose summary is ready.
        await self._write(segment)

        task = asyncio.create_task(
            self._extract(segment, summary, covered, scope, reason)
        )
        self._extractions[task] = segment
        task.add_done_callback(self._extractions.pop)

    async def _extract(
        self,
        segment: Segment,
        snapshot: list[SegmentChainNode],
        covered: list[SegmentChainNode],
        scope: SubtaskScope | None,
        reason: BoundaryReason,
    ) -> None:
        async with self._slots:
            result = await self.extractor.extract(
                snapshot,
                scope=scope,
                boundary_reason=reason,
                files=self._files_for(segment),
            )
        await self._log_extraction(segment.segment_id, covered, result)
        if result.ok:
            segment = segment.model_copy(
                update={"status": "done", **result.fields, "updated_at": _now()}
            )
        else:
            self.stats.failed += 1
            segment = segment.model_copy(
                update={"status": "failed", "updated_at": _now()}
            )
        await self._write(segment)
        if segment.status == "done" and self.on_segment is not None:
            await self.on_segment(segment)

    def _files_for(self, segment: Segment) -> dict[str, str]:
        """Return files produced inside this segment's coverage seq range.

        A segment is worth summarizing even when the file feed is unavailable, so
        a failure here costs the artifact list rather than the whole segment.
        """

        try:
            files = self.artifact_files(
                segment.coverage.start_seq, segment.coverage.end_seq
            )
        except Exception:
            logger.exception(
                "Artifact file lookup failed for %s", segment.segment_id
            )
            return {}
        if not files:
            logger.info(
                "segment %s has no in-coverage artifact file", segment.segment_id
            )
        return files

    async def _wait_extractions(self, budget: float) -> None:
        """Wait out the in-flight extractions, failing whatever overruns."""
        tasks = list(self._extractions)
        if not tasks:
            return
        _finished, pending = await asyncio.wait(tasks, timeout=budget)
        for task in pending:
            segment = self._extractions.get(task)
            task.cancel()
            if segment is None:
                continue
            self.stats.failed += 1
            logger.warning(
                "segment %s extraction timed out at close", segment.segment_id
            )
            await self._write(
                segment.model_copy(
                    update={"status": "failed", "updated_at": _now()}
                )
            )

    async def _write(self, segment: Segment) -> None:
        self._seq += 1
        self.stats.rows += 1
        await self.on_row(segment)

    # -- judge logs -------------------------------------------------------- #

    async def _log_continuity(
        self, peek: SegmentChainNode, decision: ContinuityDecision
    ) -> None:
        if self.on_judge_log is None:
            return
        await self.on_judge_log(
            {
                "type": "continuity",
                "window_start_seq": self._window[0].seqs[0]
                if self._window[0].seqs
                else None,
                "window_end_seq": self._window[-1].seqs[-1]
                if self._window[-1].seqs
                else None,
                "peek_event_id": peek.event_ids[0] if peek.event_ids else None,
                "continues": decision.continues,
                "reason": decision.reason,
            }
        )

    async def _log_extraction(
        self,
        segment_id: str,
        covered: list[SegmentChainNode],
        result: ExtractionOutcome,
    ) -> None:
        if self.on_judge_log is None:
            return
        await self.on_judge_log(
            {
                "type": "extraction",
                "segment_id": segment_id,
                "window_start_seq": covered[0].seqs[0] if covered[0].seqs else None,
                "window_end_seq": covered[-1].seqs[-1] if covered[-1].seqs else None,
                "ok": result.ok,
                "fields": result.log_fields(),
            }
        )


def _covered_nodes(window: list[SegmentChainNode]) -> list[SegmentChainNode]:
    """Return the window nodes a segment claims, dropping pure context."""
    return [node for node in window if node.covered and node.seqs]


def _is_task_state_update(node: SegmentChainNode) -> bool:
    info = node.orchestration or {}
    return node.kind == "orchestration" and info.get("category") == "TaskStateUpdate"


def _summary_nodes(window: list[SegmentChainNode]) -> list[SegmentChainNode]:
    """Nodes the continuity judge and extractor may read.

    ``TaskStateUpdate`` stays in the window for coverage (frontend fold) but is
    omitted here so bookkeeping tools do not shape the segment summary.
    """
    return [node for node in window if not _is_task_state_update(node)]


def _productive_nodes(window: list[SegmentChainNode]) -> list[SegmentChainNode]:
    """Covered nodes that justify emitting a segment (excludes TaskStateUpdate)."""
    return [node for node in _covered_nodes(window) if not _is_task_state_update(node)]


def _build_scope(info: dict[str, Any]) -> SubtaskScope | None:
    node_id = info.get("node_id")
    if not node_id:
        logger.warning("sub-task scope missing task_id/node_id: %r", info)
        return None
    graph_id = info.get("graph_id")
    return SubtaskScope(
        node_id=str(node_id),
        node_name=str(info.get("node_name") or node_id),
        graph_id=str(graph_id) if graph_id else None,
    )


def _scope_text(scope: SubtaskScope | None) -> str | None:
    if scope is None:
        return None
    return f"{scope.node_name} (node_id={scope.node_id})"


def _title_from(behavior: str) -> str:
    """Derive a title from the first sentence of the behavior summary."""
    for stop in ("。", ". ", "\n"):
        head, sep, _ = behavior.partition(stop)
        if sep:
            return head[:TITLE_CHAR_CAP]
    return behavior[:TITLE_CHAR_CAP]


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _text_field(value: Any) -> str | None:
    """Normalize an extractor text field into a single markdown string.

    A small model often ignores ``"type": "string"`` and emits a JSON array of
    steps for input / behavior / conclusion. Folding those back into markdown
    keeps a segment that is otherwise perfectly good.
    """

    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, list):
        return None
    lines: list[str] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            piece = item.strip()
        elif isinstance(item, bool | int | float):
            piece = str(item)
        else:
            continue
        if not piece:
            continue
        lines.append(piece if _LIST_MARKER_RE.match(piece) else f"{index}. {piece}")
    return "\n".join(lines).strip() or None


def _mentions_filename(text: str, filename: str) -> bool:
    """Whether ``filename`` appears as an exact path segment in ``text``."""
    if not filename:
        return False
    pattern = re.compile(rf"(?<![\w.-]){re.escape(filename)}(?![\w.-])", re.IGNORECASE)
    return pattern.search(text) is not None


def _strip_artifact_names_from_input(
    text: str | None, produced_names: set[str]
) -> str | None:
    """Drop input lines that name files produced in this window.

    Input describes what the segment consumed, not what it wrote. ``produced_names``
    is the in-window candidate set (plus any verified artifact names); lines that
    mention those filenames are removed so outputs cannot pad input. An emptied
    field becomes None.
    """

    names = {name for name in produced_names if name}
    if text is None or not names:
        return text
    kept = [
        line
        for line in text.splitlines()
        if line.strip() and not any(_mentions_filename(line, name) for name in names)
    ]
    return "\n".join(kept).strip() or None


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ArtifactFiles",
    "ChainBuilder",
    "ContinuityDecision",
    "ContinuityJudge",
    "DEFAULT_MAX_SPAN",
    "ExtractionOutcome",
    "SegmentAssembler",
    "SegmentChainNode",
    "SegmentExtractor",
    "SegmentStats",
    "no_artifact_files",
    "render_block",
    "render_window",
]
