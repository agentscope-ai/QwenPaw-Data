# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Literal

from agentscope.agent import Agent
from agentscope.event import EventType
from agentscope.state import TaskContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.tools.plan_tools import PLAN_TOOL_NAMES
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.chat_failure import ChatFailure
from qwenpaw_data.host.core.domain.exception import IterationLimitExceeded
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.integrations.cm import fetch_datasource_type
from qwenpaw_data.host.core.tracing.records import tracer_send_status, tracer_send_stop
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    McpRepository,
    PreferencesRepository,
    SessionRepository,
    SkillStateRepository,
)
from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore
from qwenpaw_data.host.core.runtime.builder import get_agent_builder
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.runtime.executor import AgentExecutor
from qwenpaw_data.host.core.query_log import Step, log_error, log_info
from qwenpaw_data.host.core.runtime.turn import TurnInput
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.utils.agent import close_interrupted_tool_calls
from qwenpaw_data.host.core.utils.plan import to_task_context
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.time import utcnow
from qwenpaw_data.host.core.workspace import get_workspace_manager

Outcome = Literal["completed", "canceled", "failed"]

logger = logging.getLogger(__name__)


class ChatRuntime:
    """Orchestrate one Chat turn on a Session-scoped agent."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_id: str,
    ) -> None:
        self.session_factory = session_factory
        self._chat_id = chat_id
        self._executor: AgentExecutor | None = None
        self._tasks: list[asyncio.Task] = []
        self._finished = asyncio.Event()
        self._cancel_requested = False
        self._agent: QwenPawDataAgent | None = None
        self._run_context: RunContext | None = None
        self._access_token: str | None = None

    @property
    def agent(self) -> QwenPawDataAgent:
        if self._agent is None:
            raise RuntimeError("CONFLICT: chat runtime agent is not ready")
        return self._agent

    async def run(
        self,
        *,
        identity: Identity,
        access_token: str | None = None,
        mcp_headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        try:
            await self._run(
                identity=identity,
                access_token=access_token,
                mcp_headers=mcp_headers or {},
            )
        finally:
            await self.clear()

    async def cancel(self, *, timeout: float | None = None) -> bool:
        """Stop the turn and wait until it has unwound.

        Returns once ``run`` has finished: the agent task and its tools are
        gone, the workspace lease is released and the terminal event is
        persisted. The wait is bounded (default: the heartbeat TTL, after which
        the chat is treated as stale anyway); returns False if it ran out.
        """
        if self._finished.is_set():
            return True
        await self._request_cancel()
        if timeout is None:
            timeout = get_settings().stream.heartbeat_ttl_seconds
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "chat %s did not unwind within %ss of cancel", self._chat_id, timeout
            )
            return False
        return True

    async def _request_cancel(self) -> None:
        self._cancel_requested = True
        if self._executor is not None:
            await self._executor.stop()

    async def clear(self) -> None:
        # 取消后台任务（watch cancel 等）
        for task in self._tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        # 关闭agent执行器
        if self._executor is not None:
            await self._executor.stop()

        # 其它
        self._finished.set()
        await get_hub().close(self._chat_id)

    def add_watch_cancel_task(self) -> None:
        async def watch() -> None:
            interval = get_settings().stream.sse_poll_seconds
            while not self._cancel_requested:
                async with self.session_factory() as db:
                    if await ChatCommandRepository(db).has_cancel(self._chat_id):
                        # Not cancel(): clear() cancels this task before it
                        # marks the run finished, so it must not wait for that.
                        await self._request_cancel()
                        return
                await asyncio.sleep(interval)

        self._tasks.append(asyncio.create_task(watch()))

    def add_heartbeat_task(self, identity: Identity) -> None:
        async def beat() -> None:
            interval = get_settings().stream.heartbeat_interval_seconds
            while not self._cancel_requested:
                try:
                    async with self.session_factory() as db:
                        await ChatRepository(db, identity).touch_heartbeat(
                            self._chat_id
                        )
                        await db.commit()
                except Exception:
                    logger.exception("chat %s heartbeat failed", self._chat_id)
                await asyncio.sleep(interval)

        self._tasks.append(asyncio.create_task(beat()))

    def add_watch_plan_task(self) -> None:
        if self._run_context is None:
            raise RuntimeError("run context is required")
        identity = self._run_context.identity

        async def watch() -> None:
            interval = get_settings().stream.sse_poll_seconds
            while not self._cancel_requested:
                async with self.session_factory() as db:
                    pending = await ChatCommandRepository(db).take_plans(self._chat_id)
                    if pending:
                        for item in pending:
                            self.agent.replace_plan(
                                to_task_context(item["plan"]),
                                reason=item["reason"],
                            )
                        await ChatRepository(db, identity).update_plan(
                            self._chat_id, self.agent.get_plan()
                        )
                        await db.commit()
                await asyncio.sleep(interval)

        self._tasks.append(asyncio.create_task(watch()))

    async def _run(
        self,
        *,
        identity: Identity,
        access_token: str | None,
        mcp_headers: dict[str, dict[str, str]],
    ) -> None:
        self._access_token = access_token
        async with self.session_factory() as db:
            chat = await ChatRepository(db, identity).get(self._chat_id)
            if chat.status != "running":
                raise ValueError(f"chat not runnable: {chat.status}")
            session = await SessionRepository(db, identity).get(chat.session_id)
            if not session.id.strip():
                raise ValueError("session_id is required")
            prefs = await PreferencesRepository(db, identity).load()
            user_runtime_config = prefs.resolve()
            disabled_skills = await SkillStateRepository(
                db, identity, agent_id=session.agent_id
            ).disabled_names()
            state = await AgentStateStore(db, identity).load(session.id)
        self.add_heartbeat_task(identity)
        self.add_watch_cancel_task()

        agent: QwenPawDataAgent | None = None
        envelope: Envelope | None = None
        outcome: Outcome = "failed"
        failure: ChatFailure | None = None
        try:
            paths = UserAgentPaths.from_identity(
                home=get_settings().paths.home,
                identity=session.identity,
                agent_id=session.agent_id,
                session_id=session.id,
            )
            paths.artifact_dir.mkdir(parents=True, exist_ok=True)
            envelope = Envelope(
                self._output_stream(chat),
                session_factory=self.session_factory,
            )
            await envelope.begin()
            async with get_workspace_manager().lease(paths) as workspace:
                datasource_id = chat.datasource_id or session.datasource_id
                dataset_names = chat.dataset_names or None
                datasource_type = await fetch_datasource_type(
                    access_token,
                    datasource_id=datasource_id,
                )
                await self._setup_mcp(
                    workspace,
                    paths=paths,
                    session_id=session.id,
                    chat_id=chat.id,
                    envelope=envelope,
                    access_token=access_token,
                    mcp_headers=mcp_headers,
                    datasource_id=datasource_id,
                    dataset_names=dataset_names,
                )
                run_context = RunContext(
                    session_id=session.id,
                    chat_id=chat.id,
                    workspace=workspace,
                    paths=paths,
                    user_runtime_config=user_runtime_config,
                    identity=identity,
                    session_factory=self.session_factory,
                    disabled_skills=disabled_skills,
                    request_context={
                        "datasource_id": datasource_id,
                        "datasource_type": datasource_type,
                        "dataset_names": dataset_names,
                        "access_token": access_token,
                        "parent_session_id": session.parent_session_id,
                    },
                )
                self._run_context = run_context
                self._executor = AgentExecutor(run_context)
                self._log_chat_start(chat, run_context)
                state.tasks_context = chat.plan
                agent = await get_agent_builder(session.agent_id).build(
                    run_context=run_context,
                    envelope=envelope,
                    state=state,
                )
                self._agent = agent
                close_interrupted_tool_calls(agent)
                self.add_watch_plan_task()
                if self._cancel_requested:
                    outcome, failure = "canceled", None
                else:
                    await self._executor.start(
                        agent,
                        TurnInput.from_chat(chat),
                        envelope,
                        after_event_callback=self._after_event_callback,
                    )
                    outcome, failure = "completed", None
        except IterationLimitExceeded as exc:
            outcome, failure = "failed", ChatFailure.iteration_limit(exc.agent_name)
        except asyncio.CancelledError:
            outcome, failure = "canceled", None
        except Exception as exc:
            logger.exception("chat %s failed", self._chat_id)
            outcome, failure = "failed", ChatFailure.from_exception(exc)

        await self._finish(chat, agent, envelope, outcome, failure=failure)

    def _output_stream(self, chat: Chat) -> OutputStream:
        return OutputStream(
            self.session_factory,
            session_id=chat.session_id,
            chat_id=chat.id,
            identity=chat.identity,
            last_sequence_number=chat.last_sequence_number,
        )

    async def _setup_mcp(
        self,
        workspace: Any,
        *,
        paths: UserAgentPaths,
        session_id: str,
        chat_id: str,
        envelope: Envelope,
        access_token: str | None,
        mcp_headers: dict[str, dict[str, str]],
        datasource_id: str | None,
        dataset_names: list[str] | None,
    ) -> None:
        await McpRepository(paths.mcp_manifest).sync(
            workspace,
            agent_id=paths.agent_id,
            session_id=session_id,
            request_id=chat_id,
            mcp_headers=mcp_headers,
            stream=envelope.stream,
            access_token=access_token,
            datasource_id=datasource_id,
            dataset_names=dataset_names,
        )

    async def _after_event_callback(self, event: Any) -> None:
        if event.type != EventType.TOOL_RESULT_START:
            return
        if event.tool_call_name not in PLAN_TOOL_NAMES:
            return
        await self._save_plan()

    async def _save_plan(self) -> None:
        ctx = self._run_context
        async with self.session_factory() as db:
            await ChatRepository(db, ctx.identity).update_plan(
                ctx.chat_id, self.agent.get_plan()
            )
            await db.commit()

    async def _finish(
        self,
        chat: Chat,
        agent: Agent | None,
        envelope: Envelope | None,
        outcome: Outcome,
        *,
        failure: ChatFailure | None = None,
    ) -> None:
        if agent is not None:
            close_interrupted_tool_calls(agent)
            chat.plan = agent.state.tasks_context
            agent.state.tasks_context = TaskContext()
            chat.agent_state = agent.state.model_dump(mode="json")

        if envelope is not None:
            await envelope.finalize()

        if outcome == "completed":
            chat.mark_status("completed")
        elif outcome == "canceled":
            chat.cancel()
        else:
            if failure is None:
                raise ValueError("failure is required")
            chat.mark_failed(failure)

        async with self.session_factory() as db:
            if agent is not None:
                await AgentStateStore(db, chat.identity).save(
                    chat.session_id, agent.state
                )
            await ChatRepository(db, chat.identity).save(chat)
            await ChatCommandRepository(db).clear(chat.id)
            if outcome == "canceled":
                await tracer_send_stop(db, chat)
            await tracer_send_status(db, chat, outcome, failure)
            await db.commit()

        if outcome == "completed":
            if envelope is None:
                raise RuntimeError("envelope is required")
            await envelope.emit_completed()
        elif outcome == "canceled":
            if envelope is not None:
                await envelope.emit_cancelled()
            else:
                await self._output_stream(chat).response_cancelled()
        else:
            if failure is None:
                raise ValueError("failure is required")
            wire = failure.to_dict()
            if envelope is not None:
                await envelope.emit_failed(error=wire)
            else:
                await self._output_stream(chat).response_failed(error=wire)

        if envelope is not None:
            await envelope.stream.flush()
        ctx = self._chat_end_context(chat, agent, outcome, failure)
        if outcome == "failed":
            log_error(
                request_id=chat.id,
                step=Step.CHAT_FAILED,
                message="Chat turn failed",
                context=ctx,
            )
            return
        log_info(
            request_id=chat.id,
            step=Step.CHAT_END,
            message="Chat turn finished",
            context=ctx,
        )

    def _log_chat_start(self, chat: Chat, run_context: RunContext) -> None:
        model = run_context.user_runtime_config.default
        cfg = run_context.user_runtime_config
        identity = run_context.identity
        log_info(
            request_id=chat.id,
            step=Step.CHAT_START,
            message="Chat turn started",
            context={
                "session_id": chat.session_id,
                "user_id": identity.user_id,
                "tenant_id": identity.tenant_id,
                "workspace_id": identity.workspace_id,
                "agent_id": run_context.paths.agent_id,
                "provider_id": model.provider_id,
                "model_id": model.model_id,
                "thinking_enabled": model.thinking_enabled,
                "context_size": model.context_size,
                "react_max_iters": cfg.react_max_iters,
                "trigger_ratio": cfg.context_trigger_ratio,
                "reserve_ratio": cfg.context_reserve_ratio,
                "datasource_id": run_context.request_context.get("datasource_id"),
                "datasource_type": run_context.request_context.get("datasource_type"),
                "sequence": chat.sequence,
                "input_chars": len(chat.user_input),
                "attachment_count": len(chat.attachments),
                "comment_count": len(chat.artifact_comments),
            },
        )

    def _chat_end_context(
        self,
        chat: Chat,
        agent: Agent | None,
        outcome: Outcome,
        failure: ChatFailure | None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "session_id": chat.session_id,
            "outcome": outcome,
            "duration_ms": int((utcnow() - chat.started_at).total_seconds() * 1000),
        }
        if agent is not None:
            ctx["react_iters"] = int(agent.state.cur_iter)
        if outcome == "failed":
            if failure is None:
                raise ValueError("failure is required")
            ctx["code"] = failure.code
            ctx["error"] = failure.cause
        return ctx
