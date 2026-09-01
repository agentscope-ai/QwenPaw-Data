# -*- coding: utf-8 -*-
"""The algorithm side of the Segment contract: one Transformer per Chat.

Everything the host sees is here: the constructor the host middleware calls, the
three lifecycle calls, and the two Envelope methods the algorithm is allowed to
touch. The conversion and segmentation logic sits behind
:class:`~qwenpaw_data.host.core.algo.biztrace.pipeline.BizTracePipeline`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.biztrace.llm import for_structured_calls
from qwenpaw_data.host.core.algo.biztrace.pipeline import BizTracePipeline, build_pipeline
from qwenpaw_data.host.core.model import build_model_from_env
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.utils.workspace import list_session_files

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

    # Typing only: the host's runtime package imports this module through its
    # middleware, so importing it back at import time would close a cycle.
    from qwenpaw_data.host.core.runtime.context import RunContext
    from qwenpaw_data.host.core.runtime.envelope import Envelope

logger = logging.getLogger(__name__)


class BizTraceTransformer:
    """Turn one Chat's raw agent events into BizEvents and Segments.

    The host drives ``start`` once, ``append`` per event, and ``join`` before
    the terminal SSE event. None of the three may block the reply, so all the
    work happens on the pipeline's own workers.
    """

    def __init__(self, *, run_context: RunContext, envelope: Envelope) -> None:
        self.run_context = run_context
        self.chat_id = run_context.chat_id
        self.session_id = run_context.session_id
        self.artifact_dir = Path(run_context.paths.artifact_dir)
        self.datasource_id = run_context.request_context.get("datasource_id")
        token = run_context.request_context.get("access_token")
        self.access_token = token if isinstance(token, str) else None
        self.envelope = envelope
        self._pipeline: BizTracePipeline | None = None
        self._flush: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Build the pipeline and spin up its workers; never raises."""
        if self._pipeline is not None:
            return
        try:
            self._pipeline = build_pipeline(
                session_id=self.session_id,
                datasource_id=self.datasource_id,
                access_token=self.access_token,
                biz_event_callback=self.envelope.send_biz_event,
                segment_callback=self.envelope.send_segment,
                lister=self._list_files,
                chat_model=self._chat_model(),
            )
        except Exception:
            logger.exception("BizTrace failed to start for chat %s", self.chat_id)

    async def append(self, entry: dict[str, Any] | None, *, last: bool = False) -> None:
        """Enqueue one raw entry; ``last`` is the EOF sentinel and carries none.

        Non-blocking by contract: the queue is bounded and drops rather than
        waits, so a slow model can never hold the reply up.
        """

        try:
            pipeline = self._pipeline
            if pipeline is not None and entry is not None:
                pipeline.on_trace_event(dict(entry))
            if last:
                # Start flushing now rather than at join, so the last segment's
                # summary runs while the host closes the turn down.
                self._begin_flush()
        except Exception:
            logger.exception("BizTrace failed to accept an entry")

    async def join(self) -> None:
        """Wait until every BizEvent and Segment has been sent."""
        self._begin_flush()
        task = self._flush
        if task is None:
            return
        # Shielded: the host caps this wait, and a cancelled wait must not take
        # the flush down with it, or the events already in the worker would die
        # with the Chat instead of reaching the host.
        await asyncio.shield(task)

    def _chat_model(self) -> ChatModelBase | None:
        """The model every step runs on, or None to stay on the rule-based path.

        ``light`` is the small model the user picked for background work; the
        default model stands in when none is configured. Without either, a
        fresh env-configured model is built — never the agent's own instance,
        which retuning would corrupt.
        """

        config = self.run_context.user_runtime_config
        active = None
        if config is not None:
            active = getattr(config, "light", None) or getattr(
                config, "default", None
            )
        try:
            if active is not None:
                return for_structured_calls(build_model(active))
            return for_structured_calls(build_model_from_env())
        except Exception:
            logger.exception(
                "BizTrace has no usable model for chat %s; using rule-based cards",
                self.chat_id,
            )
            return None

    def _begin_flush(self) -> None:
        if self._flush is None and self._pipeline is not None:
            self._flush = asyncio.create_task(self._flush_pipeline())

    async def _flush_pipeline(self) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        try:
            await pipeline.aclose()
        except Exception:
            logger.exception("BizTrace failed to flush chat %s", self.chat_id)

    def _list_files(self) -> list[dict[str, Any]]:
        return list_session_files(self.artifact_dir)


__all__ = ["BizTraceTransformer"]
