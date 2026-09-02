# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from agentscope.event import EventType

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.orchestration.tools import PLAN_TOOL_NAMES
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.runtime.executor import AgentExecutor
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.runtime.turn import TurnInput
from qwenpaw_data.host.core.store.protocols import ChatEventStore, ChatStore
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

Outcome = Literal["completed", "canceled", "failed"]

logger = logging.getLogger(__name__)


class ChatRuntime:
    """Orchestrate one Chat turn on a Session-scoped host agent."""

    def __init__(
        self,
        *,
        chats: ChatStore,
        events: ChatEventStore,
        hosts: QwenPawDataHostRegistry,
    ) -> None:
        self.chats = chats
        self.events = events
        self.hosts = hosts
        self._executor = AgentExecutor()
        self._finished = asyncio.Event()
        self._cancel_requested = False
        self._agent: Any = None
        self._run_context: RunContext | None = None
        self._stream: OutputStream | None = None

    @property
    def agent(self) -> Any:
        if self._agent is None:
            raise RuntimeError("CONFLICT: chat runtime agent is not ready")
        return self._agent

    def answer(
        self,
        *,
        clarification_id: str,
        result: dict[str, Any],
    ) -> None:
        self._executor.answer_clarification(
            clarification_id=clarification_id,
            result=result,
        )

    async def steer(self, text: str) -> None:
        """Enqueue steer text and wait until it is injected into the agent."""
        await self._executor.steer(text)

    async def run(self, chat_id: str, *, identity: Identity) -> None:
        registry = get_runtime_registry()
        registry.register(chat_id, self)
        try:
            await self._run(chat_id, identity=identity)
        finally:
            await self._executor.stop()
            registry.unregister(chat_id)
            self._finished.set()
            await get_hub().close(chat_id)

    async def cancel(self) -> None:
        if self._finished.is_set():
            return
        self._cancel_requested = True
        await self._executor.stop()
        await self._finished.wait()

    async def _run(self, chat_id: str, *, identity: Identity) -> None:
        chat = await self.chats.get(chat_id)
        if chat.status != "running":
            raise ValueError(f"chat not runnable: {chat.status}")

        envelope: Envelope | None = None
        outcome: Outcome = "failed"
        error: dict[str, Any] | None = None
        try:
            host = self.hosts.get(session_id=chat.session_id)
            stream = OutputStream(
                self.events,
                session_id=chat.session_id,
                chat_id=chat.id,
                identity=chat.identity,
            )
            self._stream = stream
            envelope = Envelope(stream)
            await envelope.begin()

            request_context = {"datasource_id": chat.datasource_id}
            agent = await host.get_agent(
                mode="agent",
                request_context=request_context,
            )
            self._agent = agent
            self._run_context = RunContext(
                session_id=chat.session_id,
                chat_id=chat.id,
                workspace=host.workspace,
                paths=host.paths,
                identity=identity,
                request_context=request_context,
            )
            if self._cancel_requested:
                outcome, error = "canceled", None
            else:
                await self._executor.start(
                    agent,
                    TurnInput.from_chat(chat),
                    envelope,
                    after_event_callback=self._after_event_callback,
                )
                outcome, error = "completed", None
        except asyncio.CancelledError:
            outcome, error = "canceled", None
        except Exception as exc:
            logger.exception("chat %s failed", chat_id)
            outcome, error = (
                "failed",
                {
                    "code": "VALIDATION",
                    "message": str(exc),
                },
            )

        await self._executor.stop()
        await self._finish(chat, envelope, outcome, error=error)

    async def _after_event_callback(self, event: Any) -> None:
        if event.type != EventType.TOOL_RESULT_START:
            return
        if event.tool_call_name not in PLAN_TOOL_NAMES:
            return
        await self._save_plan()

    async def _save_plan(self) -> None:
        ctx = self._run_context
        stream = self._stream
        if self._agent is None or ctx is None or stream is None:
            return
        try:
            snapshot = self._agent.get_plan().model_dump(mode="json")
        except RuntimeError:
            return
        await self.chats.update_plan(ctx.chat_id, snapshot)
        await stream.task_status(
            event_type="graph_updated",
            graph_snapshot=snapshot,
        )

    async def _finish(
        self,
        chat: Chat,
        envelope: Envelope | None,
        outcome: Outcome,
        *,
        error: dict[str, Any] | None = None,
    ) -> None:
        if outcome == "completed":
            await envelope.complete()
            chat.mark_status("completed")
        elif outcome == "canceled":
            if envelope is not None:
                await envelope.cancel()
            else:
                await OutputStream(
                    self.events,
                    session_id=chat.session_id,
                    chat_id=chat.id,
                    identity=chat.identity,
                ).response_cancelled()
            chat.cancel()
        else:
            failure = error or {}
            if envelope is not None:
                await envelope.fail(error=failure)
            else:
                await OutputStream(
                    self.events,
                    session_id=chat.session_id,
                    chat_id=chat.id,
                    identity=chat.identity,
                ).response_failed(error=failure)
            chat.error = {
                "code": str(failure.get("code") or "VALIDATION"),
                "message": str(failure.get("message") or "Chat execution failed"),
            }
            chat.mark_status("failed")
        await self.chats.reload_event_watermark(chat)
        await self.chats.save(chat)
