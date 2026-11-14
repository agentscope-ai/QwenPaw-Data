"""Standalone DAG behavior composed into the shared AgentScope agent.

The service's TaskContext and command-driven executor do not use this policy.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from pathlib import PurePath
from typing import Any, TYPE_CHECKING

from agentscope.event import AgentEvent, RequireUserConfirmEvent
from agentscope.message import Msg

from ..config import DEFAULT_PROMPT_DIR
from ..orchestration.middleware import (
    QwenPawDataHintMiddleware,
    QwenPawDataPromptMiddleware,
    QwenPawDataReplyMiddleware,
    QwenPawDataTraceMiddleware,
)
from ..orchestration.task_graph import (
    SOP,
    graph_nodes,
    is_graph_done,
    nodes_to_sop,
    sop_to_nodes,
)
from ..orchestration.state import RuntimeStateManager
from ..permission import ConfirmationHandler, deny_confirmation
from ..utils.msg import user_msg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .qwenpaw_data_agent import QwenPawDataAgent

ReplyStream = Callable[[Any], AsyncGenerator[AgentEvent, None]]


class DAGExecution:
    def __init__(
        self,
        runtime_state: RuntimeStateManager,
        *,
        mode: str,
        session_id: str,
        workspace_dir: PurePath,
        artifact_dir: PurePath,
        session_trace_writer: Callable[[dict[str, Any]], Awaitable[None]] | None,
        confirmation_handler: ConfirmationHandler | None,
    ) -> None:
        if mode not in {"agent", "plan"}:
            raise ValueError("mode must be agent or plan")
        self.runtime = runtime_state
        self.mode = mode
        self.session_id = session_id
        self.confirmation_handler = confirmation_handler
        self.middlewares = [
            QwenPawDataReplyMiddleware(runtime_state),
            QwenPawDataTraceMiddleware(
                runtime_state,
                session_trace_writer=session_trace_writer,
            ),
            QwenPawDataPromptMiddleware(
                mode_getter=lambda: self.mode,
                session_id=session_id,
                workspace_dir=workspace_dir,
                artifact_dir=artifact_dir,
                prompt_dir=DEFAULT_PROMPT_DIR,
            ),
            QwenPawDataHintMiddleware(runtime_state),
        ]

    def set_mode(self, agent: QwenPawDataAgent, mode: str) -> None:
        if mode not in {"agent", "plan"}:
            raise ValueError("mode must be agent or plan")
        self.mode = mode
        agent.state.tool_context.activated_groups = [mode]
        configure = getattr(agent.toolkit, "set_qwenpaw_data_mode", None)
        if configure is not None:
            configure(mode)

    def get_plan(self) -> SOP:
        rs = self.runtime
        if not rs.current_graph_id:
            raise RuntimeError("planning did not produce a task graph")
        return nodes_to_sop(rs._nodes, rs.current_graph_id, rs._graph_registry)

    async def load(self, sop: SOP | dict | str) -> SOP:
        graph_id, nodes, metadata = sop_to_nodes(sop)
        self.runtime._graph_registry[graph_id] = metadata
        await self.runtime.load_graph_from_nodes(graph_id, nodes)
        return self.get_plan()

    async def replace_plan(
        self,
        agent: QwenPawDataAgent,
        sop: SOP | dict | str,
        reason: str | None,
    ) -> SOP:
        plan = await self.load(sop)
        agent.state.context.append(agent._plan_changed_message(reason))
        return plan

    def finalize(self, msg: Msg, graph_id: str | None) -> Msg:
        rs = self.runtime
        msg.metadata.update(
            graph_id=graph_id,
            completed=bool(graph_id) and is_graph_done(rs._nodes, graph_id),
            nodes=[
                {"id": node.id, "name": node.name, "state": node.state}
                for node in graph_nodes(rs._nodes, graph_id)
            ]
            if graph_id
            else [],
            artifacts=[item.model_dump(mode="json") for item in rs.artifacts],
        )
        return msg

    async def _confirmed_reply(
        self,
        reply: ReplyStream,
        inputs: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        while True:
            confirmation = None
            async with aclosing(reply(inputs)) as events:
                async for event in events:
                    if isinstance(event, RequireUserConfirmEvent):
                        confirmation = event
                    yield event
            if confirmation is None:
                return
            inputs = deny_confirmation(confirmation)
            if self.confirmation_handler is not None:
                try:
                    answer = await self.confirmation_handler(confirmation)
                except Exception:
                    logger.warning("Standalone confirmation handler failed; denying")
                else:
                    if answer.reply_id == confirmation.reply_id:
                        inputs = answer

    async def reply_stream(
        self,
        reply: ReplyStream,
        inputs: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        rs = self.runtime
        rs.last_finished_graph_id = None
        async with aclosing(self._confirmed_reply(reply, inputs)) as events:
            async for event in events:
                yield event
        if self.mode != "agent":
            return
        graph_id = rs.current_graph_id
        for _ in range(6):
            if not graph_id or is_graph_done(rs._nodes, graph_id):
                return
            async with aclosing(
                self._confirmed_reply(
                    reply,
                    user_msg("继续执行剩余未完成的节点。"),
                )
            ) as events:
                async for event in events:
                    yield event
