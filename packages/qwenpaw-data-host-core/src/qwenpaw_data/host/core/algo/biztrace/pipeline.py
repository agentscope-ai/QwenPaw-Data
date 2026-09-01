# -*- coding: utf-8 -*-
"""One BizTrace + Trace2Segment pipeline per Chat.

The pipeline owns everything the algorithm side needs for a single run: the
converter, the segment assembler, their models and the JSONL store. The host
only calls ``on_trace_event`` for every raw event, and ``aclose`` once the run
ends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.biztrace.converter import BizTraceConverter
from qwenpaw_data.host.core.algo.biztrace.linking import build_linker
from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM
from qwenpaw_data.host.core.algo.biztrace.models import (
    BizEvent,
    FrontendEvent,
    FrontendSegment,
    Segment,
)
from qwenpaw_data.host.core.algo.biztrace.presentation import Linker, PresentationBuilder
from qwenpaw_data.host.core.algo.biztrace.segmentation import (
    ContinuityJudge,
    SegmentAssembler,
    SegmentExtractor,
)
from qwenpaw_data.host.core.algo.biztrace.settings import (
    EXTRACT_TIMEOUT_SECONDS,
    FLUSH_BUDGET_SECONDS,
    JUDGE_TIMEOUT_SECONDS,
    PRESENTATION_TIMEOUT_SECONDS,
    SEGMENT_QUEUE_SIZE,
    BizTraceSettings,
)
from qwenpaw_data.host.core.algo.biztrace.store import BizTraceStore, build_store_paths
from qwenpaw_data.host.core.algo.biztrace.workspace_index import (
    ArtifactVerifier,
    WorkspaceLister,
)

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

BizEventCallback = Callable[[dict[str, Any]], Awaitable[None]]
SegmentCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ArtifactFileIndex:
    """Pair host ``artifact_delta`` batches with the next tool_result seq.

    Host order is delta then ``TOOL_RESULT_END``. Pending files are bound to
    that call id at enqueue time (before conversion assigns a seq), then stamped
    with the result seq when the BizEvent is written. Productions are kept as a
    list so a later rewrite does not hide an earlier segment's candidates.
    """

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._by_call: dict[str, dict[str, str]] = {}
        self._files: list[tuple[str, str, int]] = []

    def note_delta(self, files: dict[str, Any]) -> None:
        for name, path in files.items():
            if name and path:
                self._pending[str(name)] = str(path)

    def bind_pending(self, tool_call_id: str) -> None:
        if not tool_call_id or not self._pending:
            return
        self._by_call[tool_call_id] = self._pending
        self._pending = {}

    def assign_seq(self, tool_call_id: str | None, seq: int) -> None:
        if not tool_call_id:
            return
        files = self._by_call.pop(tool_call_id, None)
        if not files:
            return
        for name, path in files.items():
            self._files.append((name, path, seq))

    def files_in(self, start_seq: int, end_seq: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, path, seq in self._files:
            if start_seq <= seq <= end_seq:
                out[name] = path
        return out


def _tool_result_call_id(payload: dict[str, Any]) -> str | None:
    evt_type = payload.get("type")
    if hasattr(evt_type, "value"):
        evt_type = evt_type.value
    if evt_type != "TOOL_RESULT_END":
        return None
    call_id = str(payload.get("tool_call_id") or "").strip()
    return call_id or None


class BizTracePipeline:
    """Wire a converter and an assembler onto one Chat's raw event flow."""

    def __init__(
        self,
        *,
        session_id: str,
        store: BizTraceStore,
        presenter: PresentationBuilder,
        settings: BizTraceSettings,
        judge_llm: StructuredLLM | None = None,
        extract_llm: StructuredLLM | None = None,
        lister: WorkspaceLister | None = None,
        linker: Linker | None = None,
        biz_event_callback: BizEventCallback | None = None,
        segment_callback: SegmentCallback | None = None,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.settings = settings
        self.biz_event_callback = biz_event_callback
        self.segment_callback = segment_callback
        self._artifacts = ArtifactFileIndex()

        self.converter = BizTraceConverter(
            presenter=presenter,
            on_row=self._write_event,
            on_event=self._forward_event,
            lang=settings.segment_prompt_lang,
        )
        self.assembler = (
            self._build_assembler(
                judge_llm=judge_llm,
                extract_llm=extract_llm,
                lister=lister,
                linker=linker,
            )
            if settings.trace2segment_enabled
            else None
        )

        self._segments: asyncio.Queue[BizEvent | None] = asyncio.Queue(
            maxsize=SEGMENT_QUEUE_SIZE
        )
        self._segment_worker: asyncio.Task[None] | None = None
        self._closed = False

    def _build_assembler(
        self,
        *,
        judge_llm: StructuredLLM | None,
        extract_llm: StructuredLLM | None,
        lister: WorkspaceLister | None,
        linker: Linker | None,
    ) -> SegmentAssembler:
        return SegmentAssembler(
            session_id=self.session_id,
            judge=ContinuityJudge(
                llm=judge_llm, lang=self.settings.segment_prompt_lang
            ),
            extractor=SegmentExtractor(
                llm=extract_llm,
                lang=self.settings.segment_prompt_lang,
                verifier=ArtifactVerifier(lister),
                linker=linker,
            ),
            on_row=self._write_segment,
            on_segment=self._publish_segment,
            on_judge_log=self.store.append_judge,
            max_span=self.settings.segment_max_span,
            concurrency=self.settings.segment_extract_concurrency,
            artifact_files=self.agent_files,
        )

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Spin up the converter workers and the segmentation worker."""
        self.converter.start()
        if self.assembler is not None and self._segment_worker is None:
            self._segment_worker = asyncio.create_task(self._segment_loop())

    def agent_files(self, start_seq: int, end_seq: int) -> dict[str, str]:
        """Files whose producing tool_result seq falls in ``[start_seq, end_seq]``."""
        return self._artifacts.files_in(start_seq, end_seq)

    def on_trace_event(self, entry: dict[str, Any]) -> None:
        """Algorithm entry point: take one raw entry, never block, never raise."""
        kind = entry.get("kind")
        if kind == "artifact_delta":
            files = (entry.get("payload") or {}).get("files") or {}
            if isinstance(files, dict):
                self._artifacts.note_delta(files)
            return
        if kind == "agent_event":
            payload = entry.get("payload")
            if isinstance(payload, dict):
                call_id = _tool_result_call_id(payload)
                if call_id is not None:
                    self._artifacts.bind_pending(call_id)
        self.converter.enqueue(entry)

    async def aclose(self) -> None:
        """Flush conversion first, then segmentation.

        Nothing to close after that: the model opens its provider client per
        call, so the pipeline holds no connection of its own.
        """

        if self._closed:
            return
        self._closed = True
        try:
            await self.converter.aclose()
        except Exception:
            logger.exception("BizTrace conversion failed to flush")
        await self._close_segments()

    async def _close_segments(self) -> None:
        if self.assembler is None:
            return
        self._segments.put_nowait(None)
        if self._segment_worker is not None:
            await self._segment_worker
            self._segment_worker = None
        try:
            await self.assembler.aclose(budget=FLUSH_BUDGET_SECONDS)
        except Exception:
            logger.exception("Trace2Segment failed to flush")

    # -- sinks ------------------------------------------------------------- #

    async def _write_event(self, event: BizEvent) -> None:
        """Persist one row, then hand the frontend projection to the host."""
        if event.kind == "tool_result":
            self._artifacts.assign_seq(event.block_id, event.seq)
        await self.store.append_event(
            seq=event.seq, event=event.model_dump(mode="json")
        )
        # Sub-agent rows stay server-side: they are detail of the spawning call.
        if self.biz_event_callback is None or event.channel == "subagent":
            return
        try:
            await self.biz_event_callback(
                FrontendEvent.of(event).model_dump(mode="json")
            )
        except Exception:
            logger.exception("biz_event_callback failed for %s", event.event_id)

    async def _forward_event(self, event: BizEvent) -> None:
        """Queue an emitted event for segmentation.

        Segmentation sits behind its own queue so its judgement calls can never
        stall the BizEvent writer.
        """

        if self.assembler is None:
            return
        try:
            self._segments.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("segmentation queue is full; dropped %s", event.event_id)

    async def _write_segment(self, segment: Segment) -> None:
        await self.store.append_segment(
            seq=segment.coverage.start_seq,
            segment=segment.model_dump(mode="json"),
        )

    async def _publish_segment(self, segment: Segment) -> None:
        """Deliver a finished summary; placeholder rows are never published."""
        projection = FrontendSegment.of(segment)
        if self.segment_callback is None or projection is None:
            return
        try:
            await self.segment_callback(projection.model_dump(mode="json"))
        except Exception:
            logger.exception(
                "segment_finish_callback failed for %s", segment.segment_id
            )

    async def _segment_loop(self) -> None:
        assembler = self.assembler
        if assembler is None:
            return
        while True:
            event = await self._segments.get()
            if event is None:
                return
            try:
                await assembler.feed(event)
            except Exception:
                logger.exception("Trace2Segment failed on %s", event.event_id)


def build_pipeline(
    *,
    session_id: str,
    datasource_id: str | None = None,
    access_token: str | None = None,
    biz_event_callback: BizEventCallback | None = None,
    segment_callback: SegmentCallback | None = None,
    settings: BizTraceSettings | None = None,
    lister: WorkspaceLister | None = None,
    chat_model: ChatModelBase | None = None,
) -> BizTracePipeline | None:
    """Assemble a started pipeline, or None when BizTrace is switched off.

    Synchronous: the Transformer's ``start`` must return fast, so nothing here
    waits on I/O. The vocabulary fetch and every model call happen later, on
    the pipeline's own workers.
    """

    config = settings if settings is not None else BizTraceSettings()
    if not config.biz_trace_enabled:
        return None

    if chat_model is None:
        logger.warning("No LLM configured for BizTrace; using rule-based cards only")

    # One linker for cards and segments alike: they share its vocabulary cache
    # and TTL refresh, and the feature switch turns both off together.
    linker = _linker(
        config,
        datasource_id=datasource_id,
        access_token=access_token,
    )
    presenter = PresentationBuilder(
        llm=_client(chat_model, timeout=PRESENTATION_TIMEOUT_SECONDS),
        lang=config.segment_prompt_lang,
        linker=linker,
    )
    pipeline = BizTracePipeline(
        session_id=session_id,
        store=BizTraceStore(
            build_store_paths(session_id=session_id, log_dir=config.biz_trace_log_dir)
        ),
        presenter=presenter,
        settings=config,
        judge_llm=_client(chat_model, timeout=JUDGE_TIMEOUT_SECONDS),
        extract_llm=_client(chat_model, timeout=EXTRACT_TIMEOUT_SECONDS),
        lister=lister,
        linker=linker,
        biz_event_callback=biz_event_callback,
        segment_callback=segment_callback,
    )
    pipeline.start()
    return pipeline


def _client(model: ChatModelBase | None, *, timeout: float) -> StructuredLLM | None:
    """Give one step its own budget on the model every step shares.

    A caption and a segment summary are not the same size of job, so the
    timeout is all that differs. None all the way through: a run without a
    model leaves every step on its rule-based path rather than failing the turn.
    """

    if model is None:
        return None
    return StructuredLLM(model, timeout=timeout)


def _linker(
    settings: BizTraceSettings,
    *,
    datasource_id: str | None,
    access_token: str | None = None,
) -> Linker | None:
    try:
        linker = build_linker(
            settings,
            datasource_id=datasource_id,
            access_token=access_token,
        )
    except Exception:
        logger.exception("Entity linking vocabulary is unavailable")
        return None
    return linker.link if linker is not None else None


__all__ = [
    "ArtifactFileIndex",
    "BizEventCallback",
    "BizTracePipeline",
    "SegmentCallback",
    "build_pipeline",
]
