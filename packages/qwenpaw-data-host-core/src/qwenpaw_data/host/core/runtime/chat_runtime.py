# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from agentscope.event import EventType
from agentscope.message import UserMsg

from qwenpaw_data.host.core.algo.biztrace.transformer import BizTraceTransformer
from qwenpaw_data.host.core.algo.followup.recommend import FollowUpRecommend
from qwenpaw_data.host.core.algo.settlement import SettlementManager, SettlementSettings
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
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.workspace import list_session_files

Outcome = Literal["completed", "canceled", "failed"]

FOLLOWUP_ENABLED_ENV = "QWENPAW_DATA_FOLLOWUP_ENABLED"
BIZTRACE_ENABLED_ENV = "QWENPAW_DATA_BIZ_TRACE_ENABLED"
BIZTRACE_JOIN_TIMEOUT_SECONDS = 90.0

logger = logging.getLogger(__name__)

# Keep strong references to fire-and-forget settlement runs until they finish.
_SETTLEMENT_TASKS: set[asyncio.Task] = set()


def _followup_enabled() -> bool:
    raw = (os.environ.get(FOLLOWUP_ENABLED_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "off"}


def _biztrace_enabled() -> bool:
    raw = (os.environ.get(BIZTRACE_ENABLED_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "off"}


class ChatRuntime:
    """Orchestrate one Chat turn on a Session-scoped host agent."""

    def __init__(
        self,
        *,
        chats: ChatStore,
        events: ChatEventStore,
        hosts: QwenPawDataHostRegistry,
        prefs: Any = None,
        sessions: Any = None,
        settlement: Any = None,
    ) -> None:
        self.chats = chats
        self.events = events
        self.hosts = hosts
        self.prefs = prefs
        self.sessions = sessions
        self.settlement = settlement
        self._executor = AgentExecutor()
        self._finished = asyncio.Event()
        self._cancel_requested = False
        self._agent: Any = None
        self._run_context: RunContext | None = None
        self._stream: OutputStream | None = None
        self._followup: FollowUpRecommend | None = None
        self._biztrace: BizTraceTransformer | None = None
        self._envelope: Envelope | None = None
        self._artifact_seen: dict[str, tuple[int, int]] = {}

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

    async def steer(
        self,
        text: str,
        artifact_comments: list[dict] | None = None,
    ) -> None:
        """Enqueue steer text and wait until it is injected into the agent."""
        await self._executor.steer(text, artifact_comments)

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
            self._envelope = envelope
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
            await self._load_runtime_config(identity)
            if _followup_enabled():
                await self._start_followup(chat, envelope)
            if _biztrace_enabled():
                await self._start_biztrace(chat, envelope)
            if self._cancel_requested:
                outcome, error = "canceled", None
            else:
                await self._executor.start(
                    agent,
                    TurnInput.from_chat(chat),
                    envelope,
                    after_event_callback=self._after_event_callback,
                )
                await self._deliver_biztrace()
                await self._deliver_followup(envelope)
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
        if outcome != "completed":
            # Cancel/failure path: stop collecting without waiting on results.
            if self._followup is not None:
                await self._followup.append(None, last=True)
            if self._biztrace is not None:
                await self._biztrace.append(None, last=True)
        await self._finish(chat, envelope, outcome, error=error)
        if outcome == "completed":
            self._schedule_settlement(chat, identity)

    async def _load_runtime_config(self, identity: Identity) -> None:
        """Attach the user's model preferences to the run context; never raises."""
        if self.prefs is None or self._run_context is None:
            return
        try:
            prefs = await self.prefs.load(identity.user_id)
            self._run_context.user_runtime_config = prefs.runtime_config()
        except Exception:
            logger.exception("failed to load preferences for chat runtime")

    def _schedule_settlement(self, chat: Chat, identity: Identity) -> None:
        """Fire-and-forget settlement detection after a completed turn."""
        if self.sessions is None or self.settlement is None:
            return
        try:
            settings = SettlementSettings()
            if not settings.enabled:
                return
            ctx = self._run_context
            manager = SettlementManager(
                sessions=self.sessions,
                chats=self.chats,
                events=self.events,
                cards=self.settlement,
                identity=identity,
                user_runtime_config=ctx.user_runtime_config if ctx else None,
                settings=settings,
            )
            task = asyncio.create_task(
                manager.on_chat_finish(
                    chat_id=chat.id,
                    session_id=chat.session_id,
                )
            )
            _SETTLEMENT_TASKS.add(task)
            task.add_done_callback(_SETTLEMENT_TASKS.discard)
        except Exception:
            logger.exception(
                "settlement: failed to schedule detection for chat %s", chat.id
            )

    async def _start_followup(
        self,
        chat: Chat,
        envelope: Envelope,
    ) -> None:
        try:
            followup = FollowUpRecommend(
                run_context=self._run_context,
                previous_followups=await self._load_previous_followups(chat),
                deliver=envelope.send_followup,
            )
            await followup.start()
            await followup.append(
                {
                    "kind": "user_input",
                    "payload": UserMsg(
                        name="user", content=chat.user_input
                    ).model_dump(),
                }
            )
            self._followup = followup
        except Exception:
            logger.exception("followup: failed to start for chat %s", chat.id)
            self._followup = None

    async def _start_biztrace(
        self,
        chat: Chat,
        envelope: Envelope,
    ) -> None:
        try:
            transformer = BizTraceTransformer(
                run_context=self._run_context,
                envelope=envelope,
            )
            await transformer.start()
            await transformer.append(
                {
                    "kind": "user_input",
                    "payload": UserMsg(
                        name="user", content=chat.user_input
                    ).model_dump(),
                }
            )
            self._artifact_seen = self._artifact_snapshot()
            self._biztrace = transformer
        except Exception:
            logger.exception("biztrace: failed to start for chat %s", chat.id)
            self._biztrace = None

    async def _deliver_biztrace(self) -> None:
        """Cap the wait here: the algorithm's join is shielded and has no limit."""
        biztrace = self._biztrace
        if biztrace is None:
            return
        try:
            await biztrace.append(None, last=True)
            await asyncio.wait_for(
                biztrace.join(), timeout=BIZTRACE_JOIN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning(
                "biztrace: join timed out after %ss for chat %s; "
                "completing with raw trace only",
                BIZTRACE_JOIN_TIMEOUT_SECONDS,
                biztrace.chat_id,
            )
        except Exception:
            logger.exception("biztrace: delivery failed")

    async def _deliver_followup(self, envelope: Envelope) -> None:
        followup = self._followup
        if followup is None:
            return
        try:
            await followup.append(None, last=True)
            questions = await followup.join()
            if questions:
                await envelope.send_followup(questions)
        except Exception:
            logger.exception("followup: delivery failed")

    async def _load_previous_followups(self, chat: Chat) -> tuple[str, ...]:
        """Questions recommended earlier in this Session, for dedup."""
        try:
            chats = await self.chats.list_for_session(chat.session_id)
            questions: list[str] = []
            for prior in chats:
                if prior.id == chat.id:
                    continue
                for obj in await self.events.read_after(prior.id, -1):
                    if obj.object == "followup.generated":
                        questions.extend(obj.followup.questions)
            return tuple(questions)
        except Exception:
            logger.exception(
                "followup: failed to load previous followups for session %s",
                chat.session_id,
            )
            return ()

    async def _after_event_callback(self, event: Any) -> None:
        if self._biztrace is not None and self._is_tool_success(event):
            # Delta first: the pipeline binds pending files to the coming
            # TOOL_RESULT_END, so order matters.
            await self._register_new_files()
        if hasattr(event, "model_dump"):
            entry = {"kind": "agent_event", "payload": event.model_dump()}
            if self._followup is not None:
                await self._followup.append(dict(entry))
            if self._biztrace is not None:
                await self._biztrace.append(dict(entry))
        if event.type != EventType.TOOL_RESULT_START:
            return
        if event.tool_call_name not in PLAN_TOOL_NAMES:
            return
        await self._save_plan()

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
        """Diff the artifact directory; notify the stream and the algorithm."""
        biztrace = self._biztrace
        envelope = self._envelope
        if biztrace is None or envelope is None:
            return
        try:
            current = self._artifact_snapshot()
            changed = [
                path
                for path, stamp in current.items()
                if self._artifact_seen.get(path) != stamp
            ]
            self._artifact_seen = current
            if not changed:
                return
            now = datetime.now(timezone.utc)
            files: dict[str, str] = {}
            for path in changed:
                name = Path(path).name
                await envelope.stream.artifact_registered(
                    id=create_id("artifact"),
                    name=name,
                    path=path,
                    created_at=now,
                    updated_at=now,
                )
                files[name] = path
            await biztrace.append(
                {"kind": "artifact_delta", "payload": {"files": files}}
            )
        except Exception:
            logger.exception(
                "biztrace: failed to register artifact files for chat %s",
                biztrace.chat_id,
            )

    def _artifact_snapshot(self) -> dict[str, tuple[int, int]]:
        ctx = self._run_context
        if ctx is None:
            return {}
        artifact_dir = Path(ctx.paths.artifact_dir)
        stamps: dict[str, tuple[int, int]] = {}
        for item in list_session_files(artifact_dir):
            path = item["rel_path"]
            try:
                stat = (artifact_dir / path).stat()
            except OSError:
                continue
            stamps[path] = (stat.st_size, stat.st_mtime_ns)
        return stamps

    async def replace_plan(
        self,
        plan: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Replace the live plan wholesale; persists and streams the snapshot."""
        agent = self.agent
        await agent.replace_plan(plan, reason=reason)
        return await self._save_plan() or {}

    async def _save_plan(self) -> dict[str, Any] | None:
        ctx = self._run_context
        stream = self._stream
        if self._agent is None or ctx is None or stream is None:
            return None
        try:
            snapshot = self._agent.get_plan().model_dump(mode="json")
        except RuntimeError:
            return None
        if not snapshot.get("nodes"):
            # finish_plan archives the graph; keep the last real plan
            # visible instead of clobbering it with an empty snapshot.
            return None
        states = self._agent.get_plan_states()
        for node in snapshot.get("nodes") or []:
            state = states.get(node.get("node_id"))
            if state is not None:
                node["state"] = state
        await self.chats.update_plan(ctx.chat_id, snapshot)
        await stream.task_status(
            event_type="graph_updated",
            graph_snapshot=snapshot,
        )
        return snapshot

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
