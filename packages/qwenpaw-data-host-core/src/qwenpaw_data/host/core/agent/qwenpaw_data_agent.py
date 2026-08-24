"""Standalone QwenPaw Data agent (AgentScope 2.0)."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.event import (
    AgentEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import AssistantMsg, Msg, TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.workspace import Offloader

from ..config import DEFAULT_PROMPT_DIR
from ..mcp_cm import inject_datasource_metadata
from ..orchestration import DefaultGraphToHint, RuntimeStateManager
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
from ..permission import ConfirmationHandler, deny_confirmation
from ..utils.msg import user_msg
from .mcp_client_log import log_mcp_client_event
from .tool_diagnostics import log_awaiting_tool_calls

logger = logging.getLogger(__name__)

_MAX_DRIVE_ROUNDS = 6


class QwenPawDataAgent(Agent):
    """Standalone QwenPaw Data agent built on AgentScope 2.0 ``Agent``."""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: ChatModelBase,
        toolkit: Toolkit,
        middlewares: list[MiddlewareBase] | None = None,
        state: AgentState | None = None,
        offloader: Offloader | None = None,
        model_config: ModelConfig = ModelConfig(),
        context_config: ContextConfig = ContextConfig(),
        react_config: ReActConfig = ReActConfig(max_iters=100),
        *,
        runtime_state: RuntimeStateManager | None = None,
        request_context: dict[str, Any] | None = None,
        mode: str = "agent",
        session_id: str = "default",
        session_trace_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self._mode: Literal["plan", "agent"] = mode  # type: ignore[assignment]
        self._session_id = session_id
        self._runtime_state = runtime_state or RuntimeStateManager(
            graph_to_hint=DefaultGraphToHint(),
        )
        self._request_context = dict(request_context or {})
        self._session_trace_writer = session_trace_writer
        self._confirmation_handler = confirmation_handler
        self._cm_mcp_tool_prefixes = set(
            getattr(toolkit, "_qwenpaw_data_cm_mcp_tool_prefixes", set()),
        )

        if state is None:
            state = AgentState(
                permission_context=PermissionContext(
                    mode=PermissionMode.DONT_ASK,
                ),
            )

        middlewares = (middlewares or []) + self._build_middlewares()

        super().__init__(
            name=name,
            system_prompt=system_prompt,
            model=model,
            toolkit=toolkit,
            middlewares=middlewares,
            state=state,
            offloader=offloader,
            model_config=model_config,
            context_config=context_config,
            react_config=react_config,
        )

        self.set_mode(mode)

    @property
    def plan_notebook(self) -> RuntimeStateManager:
        """Backward-compatible alias for _runtime_state."""
        return self._runtime_state

    def set_mode(self, new_mode: str) -> None:
        if new_mode not in ("agent", "plan"):
            logger.warning("QwenPawDataAgent.set_mode: invalid mode %r", new_mode)
            return
        self._mode = new_mode  # type: ignore[assignment]
        self.state.tool_context.activated_groups = [new_mode]
        set_qwenpaw_data_mode = getattr(self.toolkit, "set_qwenpaw_data_mode", None)
        if callable(set_qwenpaw_data_mode):
            set_qwenpaw_data_mode(new_mode)

    def set_request_context(self, request_context: dict[str, Any] | None) -> None:
        self._request_context = dict(request_context or {})

    def get_plan(self) -> SOP:
        """返回当前规划结果。"""
        rs = self._runtime_state
        graph_id = rs.current_graph_id
        if not graph_id:
            raise RuntimeError(
                "planning did not produce a task graph; "
                "check the model output and plan-mode prompt.",
            )
        return nodes_to_sop(rs._nodes, graph_id, rs._graph_registry)

    async def execute_sop(
        self,
        sop: SOP | dict | str,
        *,
        stream: bool = False,
    ) -> Msg | AsyncGenerator[AgentEvent, None]:
        """加载 SOP 并驱动当前 agent 执行到完成。"""

        async def event_stream() -> AsyncGenerator[AgentEvent, None]:
            rs = self._runtime_state
            graph_id, nodes, graph_meta = sop_to_nodes(sop)
            rs._graph_registry[graph_id] = graph_meta
            await rs.load_graph_from_nodes(graph_id, nodes)
            async for event in self.reply_stream(
                user_msg(
                    "请按当前已加载的执行计划（current_plan）逐步执行，"
                    "完成每个节点后用 update_subtask 记录进度，直到所有节点完成。",
                ),
            ):
                yield event

        if stream:
            return event_stream()

        msg = await self._collect_reply(event_stream())
        return self._finalize_reply(msg)

    async def reply(self, inputs: Any = None) -> Msg:
        msg = await self._collect_reply(self.reply_stream(inputs))
        return self._finalize_reply(msg)

    async def reply_stream(
        self,
        inputs: Any = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        try:
            async for event in self._reply_with_confirmations(inputs):
                yield event
        except Exception:
            log_awaiting_tool_calls(
                agent_name=self.name,
                state=self.state,
                source="reply_stream",
            )
            raise

        if self._mode != "agent":
            return

        graph_id = self._runtime_state.current_graph_id
        if graph_id and not is_graph_done(self._runtime_state._nodes, graph_id):
            async for event in self._continue_current_graph_stream(graph_id):
                yield event

    async def _reply_with_confirmations(
        self,
        inputs: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Drive AgentScope confirmation events without leaving a run parked."""
        pending_inputs = inputs
        while True:
            confirmation: RequireUserConfirmEvent | None = None
            async for event in super().reply_stream(pending_inputs):
                if isinstance(event, RequireUserConfirmEvent):
                    confirmation = event
                yield event

            if confirmation is None:
                return

            result = await self._resolve_confirmation(confirmation)
            pending_inputs = result

    async def _resolve_confirmation(
        self,
        event: RequireUserConfirmEvent,
    ) -> UserConfirmResultEvent:
        if self._confirmation_handler is None:
            logger.warning(
                "permission confirmation requested without a handler; denying",
            )
            return deny_confirmation(event)
        try:
            result = await self._confirmation_handler(event)
        except Exception:
            logger.warning(
                "permission confirmation handler failed; denying",
                exc_info=True,
            )
            return deny_confirmation(event)
        if result.reply_id != event.reply_id:
            logger.warning(
                "permission confirmation handler returned a mismatched reply id; "
                "denying",
            )
            return deny_confirmation(event)
        return result

    async def _collect_reply(
        self,
        events: AsyncGenerator[AgentEvent, None],
    ) -> Msg:
        final_msg: Msg | None = None
        current_msg: Msg | None = None

        async for event in events:
            if isinstance(event, ReplyStartEvent):
                current_msg = AssistantMsg(
                    name=event.name,
                    content=[],
                    id=event.reply_id,
                )
                final_msg = current_msg
            elif current_msg is not None and hasattr(event, "reply_id"):
                current_msg.append_event(event)

        if final_msg is None:
            raise RuntimeError("QwenPawDataAgent did not produce a final message.")

        return final_msg

    def _finalize_reply(self, msg: Msg) -> Msg:
        if self._mode != "agent":
            return msg

        graph_id = self._runtime_state.current_graph_id
        return self.set_execution_metadata(msg, graph_id=graph_id)

    async def handle_interrupt(
        self,
        msg: Msg | list[Msg] | None = None,
        **kwargs: Any,
    ) -> Msg:
        _ = (msg, kwargs)
        gn = self._runtime_state._graph_nodes()
        if gn:
            from ..orchestration.task_graph import graph_to_markdown

            graph_md = graph_to_markdown(
                self._runtime_state._nodes,
                self._runtime_state._current_graph_id or "",
            )
            text = (
                f"任务已暂停。当前进度：\n{graph_md}\n\n"
                "你可以：\n"
                "- 直接说「继续」恢复执行\n"
                "- 告诉我需要修改的内容\n"
                "- 在任务面板中修改后点击继续"
            )
        else:
            text = "已中断。有什么需要调整的吗？"

        return Msg(
            name=self.name,
            content=[TextBlock(type="text", text=text)],
            role="assistant",
            metadata={"_is_interrupted": True},
        )

    def set_execution_metadata(self, msg: Msg, *, graph_id: str | None) -> Msg:
        rs = self._runtime_state
        msg.metadata["graph_id"] = graph_id
        msg.metadata["completed"] = bool(graph_id) and is_graph_done(rs._nodes, graph_id)
        msg.metadata["nodes"] = [
            {"id": n.id, "name": n.name, "state": n.state}
            for n in graph_nodes(rs._nodes, graph_id)
        ] if graph_id else []
        msg.metadata["artifacts"] = [
            item.model_dump(mode="json") for item in rs.artifacts
        ]
        return msg

    async def _continue_current_graph_stream(
        self,
        graph_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        rs = self._runtime_state
        try:
            async for event in self._reply_with_confirmations(
                user_msg("继续执行剩余未完成的节点。"),
            ):
                yield event
        except Exception:
            log_awaiting_tool_calls(
                agent_name=self.name,
                state=self.state,
                source="continue_current_graph",
            )
            raise
        rounds = 1
        while (
            not is_graph_done(rs._nodes, graph_id)
            and rounds < _MAX_DRIVE_ROUNDS
        ):
            try:
                async for event in self._reply_with_confirmations(
                    user_msg("继续执行剩余未完成的节点。"),
                ):
                    yield event
            except Exception:
                log_awaiting_tool_calls(
                    agent_name=self.name,
                    state=self.state,
                    source="continue_current_graph",
                )
                raise
            rounds += 1

    def _inject_datasource_metadata(self, tool_call: Any) -> None:
        inject_datasource_metadata(
            tool_call,
            request_context=self._request_context,
            cm_mcp_tool_prefixes=self._cm_mcp_tool_prefixes,
        )

    async def _execute_tool_call(
        self,
        tool_call: Any,
        kept_rules: list[Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Inject CM metadata before AgentScope validates tool inputs."""
        self._inject_datasource_metadata(tool_call)
        tool_name = str(getattr(tool_call, "name", "") or "")
        if not tool_name.startswith("mcp__"):
            async for item in super()._execute_tool_call(tool_call, kept_rules):
                yield item
            return

        # MCP tool call: mirror DataBridge's protocol log on the client side
        # so a hang can be attributed by comparing both logs.
        call_id = str(getattr(tool_call, "id", "") or "")
        log_mcp_client_event(
            "MCP_TOOL_CALL_START tool=%s tool_call_id=%s", tool_name, call_id,
        )
        t0 = time.monotonic()
        try:
            async for item in super()._execute_tool_call(tool_call, kept_rules):
                yield item
        except BaseException as exc:
            log_mcp_client_event(
                "MCP_TOOL_CALL_END tool=%s tool_call_id=%s %dms error=%s: %s",
                tool_name,
                call_id,
                int((time.monotonic() - t0) * 1000),
                type(exc).__name__,
                exc,
            )
            raise
        log_mcp_client_event(
            "MCP_TOOL_CALL_END tool=%s tool_call_id=%s %dms",
            tool_name,
            call_id,
            int((time.monotonic() - t0) * 1000),
        )

    def _build_middlewares(self) -> list[MiddlewareBase]:
        """Build the QwenPaw Data middleware stack."""
        rs = self._runtime_state
        return [
            QwenPawDataReplyMiddleware(rs),
            QwenPawDataTraceMiddleware(
                rs,
                session_trace_writer=self._session_trace_writer,
            ),
            QwenPawDataPromptMiddleware(
                mode_getter=lambda: self._mode,
                session_id=self._session_id,
                prompt_dir=DEFAULT_PROMPT_DIR,
            ),
            QwenPawDataHintMiddleware(rs),
        ]
