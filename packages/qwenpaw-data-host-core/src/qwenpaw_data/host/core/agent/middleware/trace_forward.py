# -*- coding: utf-8 -*-
"""Forward one reply's events into the BizTraceTransformer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.agent import Agent
from agentscope.event import EventType, ReplyEndEvent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.algo.biztrace.transformer import BizTraceTransformer
from qwenpaw_data.host.core.tracing.adapters import tracer_span
from qwenpaw_data.tracing.core.names import Step
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.workspace import list_session_files

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext
    from qwenpaw_data.host.core.runtime.envelope import Envelope

logger = logging.getLogger(__name__)


def _swallow_task_failure(task: asyncio.Task[None]) -> None:
    """Retrieve the abort-close result so failures are never 'never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("BizTrace abort-close task failed", exc_info=exc)


class TraceForwardMiddleware(MiddlewareBase):
    """Pass through reply events; forward user/agent/artifact entries; register chat artifacts on tool success."""

    def __init__(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
    ) -> None:
        self._envelope = envelope
        self._artifact_dir = Path(run_context.paths.artifact_dir)
        self._seen = self._snapshot()
        self._transformer = BizTraceTransformer(
            run_context=run_context, envelope=envelope
        )
        self._abort_task: asyncio.Task[None] | None = None

    @classmethod
    def find(cls, agent: Agent) -> TraceForwardMiddleware | None:
        for middleware in agent._reply_middlewares:
            if isinstance(middleware, cls):
                return middleware
        return None

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        await self._transformer.start()
        inputs = input_kwargs.get("inputs")
        msgs = inputs if isinstance(inputs, list) else [inputs]
        for msg in msgs:
            # only the first, real user message may become a user_input entry.
            if isinstance(msg, Msg):
                await self._transformer.append(
                    {"kind": "user_input", "payload": msg.model_dump()}
                )
        ended = False
        try:
            async for event in next_handler():
                if isinstance(event, ReplyEndEvent):
                    ended = True
                if self._is_tool_success(event):
                    await self._register_new_files()
                if hasattr(event, "model_dump"):
                    await self._transformer.append(
                        {"kind": "agent_event", "payload": event.model_dump()}
                    )
                yield event
        except GeneratorExit:
            # Clarification pause: chat still running, keep collecting.
            raise
        except BaseException:
            # Reply died mid-stream: close off-task so the biztrace span ends.
            self._schedule_abort_close()
            raise
        finally:
            if ended:
                await self.end_stream()
                await self._join()

    async def end_stream(self) -> None:
        await self._transformer.append(None, last=True)

    def _schedule_abort_close(self) -> None:
        """Close the pipeline off-task when the reply died mid-stream.
        Out-of-stream stops are closed by ``AgentExecutor.stop``; the flush is idempotent.
        """
        if self._abort_task is not None and not self._abort_task.done():
            return
        self._abort_task = asyncio.create_task(self._close_after_abort())
        self._abort_task.add_done_callback(_swallow_task_failure)

    async def _close_after_abort(self) -> None:
        """Best-effort BizTrace flush when the reply died without ReplyEndEvent."""
        try:
            await self.end_stream()
            await self._join()
        except Exception:
            logger.exception("BizTrace abort-close failed")

    async def _join(self) -> None:
        """Cap the wait here: the algorithm's join is shielded and has no limit."""
        timeout = get_settings().runtime.transformer_join_timeout_s
        try:
            await asyncio.wait_for(self._transformer.join(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "transformer join timed out after %ss for chat %s; "
                "completing with raw trace only",
                timeout,
                getattr(self._transformer, "chat_id", "?"),
            )

    @staticmethod
    def _is_tool_success(event: Any) -> bool:
        evt_type = getattr(event, "type", None)
        if hasattr(evt_type, "value"):
            evt_type = evt_type.value
        if evt_type != EventType.TOOL_RESULT_END.value:
            return False
        tool_state = getattr(event, "state", None)
        if hasattr(tool_state, "value"):
            tool_state = tool_state.value
        return tool_state == "success"

    async def _register_new_files(self) -> None:
        """Diff the artifact directory; index and notify the algorithm."""
        try:
            current = self._snapshot()
            changed = [
                path for path, stamp in current.items() if self._seen.get(path) != stamp
            ]
            self._seen = current
            if not changed:
                return
            files: dict[str, str] = {}
            for path in changed:
                name = Path(path).name
                registered = await self._envelope.stream.artifact_registered(
                    name=name,
                    path=path,
                )
                artifact = getattr(registered, "artifact", None)
                if artifact is not None:
                    artifact_id = getattr(artifact, "id", "")
                    with tracer_span(
                        Step.ARTIFACT,
                        attributes={
                            "artifact.id": artifact_id
                            if isinstance(artifact_id, str)
                            else "",
                            "artifact.name": name,
                            "artifact.path": str((self._artifact_dir / path).resolve()),
                        },
                    ):
                        pass
                files[name] = path
            await self._transformer.append(
                {"kind": "artifact_delta", "payload": {"files": files}}
            )
        except Exception:
            logger.exception(
                "failed to register artifact files for chat %s",
                getattr(self._transformer, "chat_id", "?"),
            )

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        stamps: dict[str, tuple[int, int]] = {}
        for item in list_session_files(self._artifact_dir):
            path = item["rel_path"]
            resolved = self._artifact_dir / path
            try:
                stat = resolved.stat()
            except OSError:
                continue
            stamps[path] = (stat.st_size, stat.st_mtime_ns)
        return stamps
